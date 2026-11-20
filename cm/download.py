"""Download con ripresa, retry, checksum e decompressione sparsa."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

import requests

from . import USER_AGENT
from .auth import ArchiveAuth, apply as apply_auth

Progress = Callable[[int, int], None]  # (fatti, totale) in byte; totale 0 = ignoto
CHUNK = 1024 * 1024
# Download a pezzi in parallelo. Misurato su archive.org: una connessione ~0,09 MB/s, quattro
# ~0,6 MB/s (circa 7 volte di piu'), otto non migliorano ulteriormente.
DEFAULT_CONNECTIONS = 4
CHUNK_BYTES = 8 * 1024 * 1024        # dimensione di un pezzo: e' anche l'unita' di ripresa
PARALLEL_MIN = 16 * 1024 * 1024      # sotto questa dimensione non vale la pena
# Risposte che non cambiano riprovando: inutile aspettare e ritentare.
FATAL_STATUS = {
    401: "accesso negato (401): l'item e' ad accesso riservato e richiede il login di archive.org",
    403: "accesso vietato (403): il file non e' scaricabile senza autorizzazione",
    404: "file non trovato (404): controlla il link o il nome del file",
    410: "file rimosso (410)",
}


class DownloadError(RuntimeError):
    pass


class _RangeUnsupported(Exception):
    """Il server non rispetta davvero le richieste a intervalli: si torna al download semplice."""


def _raise_if_fatal(e: requests.HTTPError, url: str, auth: ArchiveAuth | None) -> None:
    status = e.response.status_code if e.response is not None else None
    if status == 401 and auth:
        raise DownloadError(
            "accesso negato (401) anche con il tuo login: l'account non ha i permessi "
            f"per questo item (alcuni si possono solo prendere in prestito): {url}") from e
    if status in FATAL_STATUS:
        raise DownloadError(f"{FATAL_STATUS[status]}: {url}") from e


def filename_from_url(url: str) -> str:
    name = Path(unquote(urlparse(url).path)).name
    if not name or name in (".", ".."):
        raise ValueError(f"impossibile ricavare un nome file da {url}")
    return name


def _session(auth: ArchiveAuth | None = None) -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return apply_auth(s, auth)


def remote_size(url: str, session: requests.Session | None = None,
                auth: ArchiveAuth | None = None) -> int | None:
    s = session or _session(auth)
    try:
        r = s.head(url, allow_redirects=True, timeout=30)
        if r.status_code == 200 and "content-length" in r.headers:
            return int(r.headers["content-length"])
    except requests.RequestException:
        pass
    return None


def _probe_ranges(s: requests.Session, url: str, auth: ArchiveAuth | None) -> int | None:
    """Dimensione totale se il server accetta Range (206 + Content-Range), altrimenti None."""
    try:
        with s.get(url, headers={"Range": "bytes=0-0"}, stream=True, timeout=(15, 30)) as r:
            r.raise_for_status()
            if r.status_code == 206:
                m = re.match(r"bytes 0-0/(\d+)$", r.headers.get("Content-Range", "").strip())
                return int(m.group(1)) if m else None
            return None                      # 200: il server ignora Range
    except requests.HTTPError as e:
        _raise_if_fatal(e, url, auth)
        return None
    except requests.RequestException:
        return None


def _download_parallel(url: str, dest: Path, size: int, progress: Progress | None,
                       retries: int, auth: ArchiveAuth | None, connections: int,
                       chunk_size: int) -> Path:
    """Scarica a pezzi con piu' connessioni; riprende dai pezzi gia' completati.

    Il file parziale e' `dest.part` (preallocato); `dest.part.state` elenca i pezzi finiti."""
    part = dest.with_name(dest.name + ".part")
    state = dest.with_name(dest.name + ".part.state")
    n_chunks = -(-size // chunk_size)

    done: set[int] = set()
    try:
        st = json.loads(state.read_text(encoding="utf-8"))
        if (st["size"] == size and st["chunk"] == chunk_size and part.stat().st_size == size):
            done = {int(i) for i in st["done"] if 0 <= int(i) < n_chunks}
    except (OSError, ValueError, KeyError, TypeError):
        done = set()
    if not done:
        with open(part, "wb") as f:
            f.truncate(size)

    def bounds(idx: int) -> tuple[int, int]:
        start = idx * chunk_size
        return start, min(size, start + chunk_size) - 1

    lock = threading.Lock()
    counters = {"bytes": sum(bounds(i)[1] - bounds(i)[0] + 1 for i in done)}
    pending: queue.Queue[int] = queue.Queue()
    for idx in range(n_chunks):
        if idx not in done:
            pending.put(idx)
    stop = threading.Event()
    errors: list[BaseException] = []

    def save_state() -> None:                          # chiamata con `lock` preso
        tmp = state.with_name(state.name + ".tmp")
        tmp.write_text(json.dumps({"size": size, "chunk": chunk_size, "done": sorted(done)}),
                       encoding="utf-8")
        os.replace(tmp, state)

    def fetch_chunk(s: requests.Session, f, idx: int) -> None:
        start, end = bounds(idx)
        pos, attempt = start, 0
        while pos <= end and not stop.is_set():
            err: Exception | None = None
            try:
                with s.get(url, headers={"Range": f"bytes={pos}-{end}"}, stream=True,
                           timeout=(15, 60)) as r:
                    r.raise_for_status()
                    m = re.match(r"bytes (\d+)-", r.headers.get("Content-Range", ""))
                    if r.status_code != 206 or not m or int(m.group(1)) != pos:
                        raise _RangeUnsupported()
                    f.seek(pos)
                    for data in r.iter_content(CHUNK):
                        if stop.is_set():
                            return
                        data = data[: end + 1 - pos]
                        f.write(data)
                        pos += len(data)
                        with lock:
                            counters["bytes"] += len(data)
                        if pos > end:
                            break
            except requests.HTTPError as e:
                _raise_if_fatal(e, url, auth)
                err = e
            except (requests.RequestException, OSError) as e:
                err = e
            if pos <= end:                             # pezzo incompleto: si riprende da `pos`
                attempt += 1
                if attempt >= retries:
                    raise DownloadError(
                        f"download fallito dopo {retries} tentativi: {url}: {err or 'risposta troncata'}")
                time.sleep(2 ** (attempt - 1))
        if pos > end:
            f.flush()
            with lock:
                done.add(idx)
                save_state()

    def worker() -> None:
        s = _session(auth)                             # una sessione per thread
        try:
            with open(part, "r+b") as f:
                while not stop.is_set():
                    try:
                        idx = pending.get_nowait()
                    except queue.Empty:
                        return
                    fetch_chunk(s, f, idx)
        except BaseException as e:                     # la prima causa viene rilanciata dal chiamante
            with lock:
                errors.append(e)
            stop.set()

    if not state.exists() or not done:
        with lock:
            save_state()
    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(max(1, min(connections, pending.qsize())))]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        for t in threads:
            t.join(timeout=0.2 / len(threads))
        if progress:
            with lock:
                got = counters["bytes"]
            progress(min(got, size), size)
    if errors:
        raise errors[0]
    if len(done) != n_chunks:
        raise DownloadError(f"download incompleto ({len(done)}/{n_chunks} pezzi): {url}")
    if progress:
        progress(size, size)
    os.replace(part, dest)
    state.unlink(missing_ok=True)
    return dest


def download(url: str, dest: Path, progress: Progress | None = None,
             retries: int = 4, auth: ArchiveAuth | None = None,
             connections: int = DEFAULT_CONNECTIONS, chunk_size: int = CHUNK_BYTES,
             parallel_min: int = PARALLEL_MIN) -> Path:
    """Scarica `url` in `dest`; riprende da `dest.part` se esiste.

    Con piu' connessioni e un server che accetta gli intervalli (verificato con una richiesta
    di prova) scarica a pezzi in parallelo; altrimenti scarica in sequenza.
    `auth` (login archive.org) vale solo per archive.org e i suoi sottodomini."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    s = _session(auth)
    expected = remote_size(url, s)
    if dest.exists() and expected is not None and dest.stat().st_size == expected:
        if progress:
            progress(expected, expected)
        return dest

    part = dest.with_name(dest.name + ".part")
    state = dest.with_name(dest.name + ".part.state")
    # un .part senza .state viene da un download sequenziale: lo si completa in sequenza
    legacy_partial = part.exists() and not state.exists()
    if connections > 1 and not legacy_partial and (expected is None or expected >= parallel_min):
        size = _probe_ranges(s, url, auth)
        if size is not None and size >= parallel_min:
            try:
                return _download_parallel(url, dest, size, progress, retries, auth,
                                          connections, chunk_size)
            except _RangeUnsupported:
                part.unlink(missing_ok=True)
                state.unlink(missing_ok=True)

    last_err: Exception | None = None
    for attempt in range(retries):
        offset = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            with s.get(url, headers=headers, stream=True, timeout=(15, 60)) as r:
                if r.status_code == 416:  # gia' completo
                    break
                r.raise_for_status()
                if r.status_code == 200:  # il server ignora Range: si riparte
                    offset = 0
                total = offset + int(r.headers.get("content-length", 0) or 0)
                done = offset
                with open(part, "ab" if offset else "wb") as f:
                    for chunk in r.iter_content(CHUNK):
                        f.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
            break
        except requests.HTTPError as e:
            _raise_if_fatal(e, url, auth)
            last_err = e
            time.sleep(2 ** attempt)
        except (requests.RequestException, OSError) as e:
            last_err = e
            time.sleep(2 ** attempt)
    else:
        raise DownloadError(f"download fallito dopo {retries} tentativi: {url}: {last_err}")

    os.replace(part, dest)
    return dest


def md5_file(path: Path, progress: Progress | None = None) -> str:
    h = hashlib.md5()
    total = path.stat().st_size
    done = 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    return h.hexdigest()


def gunzip_sparse(src: Path, dst: Path, progress: Progress | None = None) -> int:
    """Decomprime src in dst saltando i blocchi di zeri (file sparso).

    Ritorna la dimensione logica del file decompresso.
    """
    total = src.stat().st_size
    with open(src, "rb") as raw, gzip.GzipFile(fileobj=raw) as gz, open(dst, "wb") as out:
        while chunk := gz.read(CHUNK):
            if not any(chunk):
                out.seek(len(chunk), os.SEEK_CUR)
            else:
                out.write(chunk)
            if progress:
                progress(raw.tell(), total)
        size = out.tell()
        out.truncate(size)
    return size
