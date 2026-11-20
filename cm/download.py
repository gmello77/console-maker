"""Download con ripresa, retry, checksum e decompressione sparsa."""
from __future__ import annotations

import gzip
import hashlib
import os
import time
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

import requests

from . import USER_AGENT
from .auth import ArchiveAuth, apply as apply_auth

Progress = Callable[[int, int], None]  # (fatti, totale) in byte; totale 0 = ignoto
CHUNK = 1024 * 1024
# Risposte che non cambiano riprovando: inutile aspettare e ritentare.
FATAL_STATUS = {
    401: "accesso negato (401): l'item e' ad accesso riservato e richiede il login di archive.org",
    403: "accesso vietato (403): il file non e' scaricabile senza autorizzazione",
    404: "file non trovato (404): controlla il link o il nome del file",
    410: "file rimosso (410)",
}


class DownloadError(RuntimeError):
    pass


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


def download(url: str, dest: Path, progress: Progress | None = None,
             retries: int = 4, auth: ArchiveAuth | None = None) -> Path:
    """Scarica `url` in `dest`; riprende da `dest.part` se esiste.

    `auth` (login archive.org) vale solo per archive.org e i suoi sottodomini."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    s = _session(auth)
    expected = remote_size(url, s)
    if dest.exists() and expected is not None and dest.stat().st_size == expected:
        if progress:
            progress(expected, expected)
        return dest

    part = dest.with_name(dest.name + ".part")
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
            status = e.response.status_code if e.response is not None else None
            if status == 401 and auth:
                raise DownloadError(
                    "accesso negato (401) anche con il tuo login: l'account non ha i permessi "
                    f"per questo item (alcuni si possono solo prendere in prestito): {url}") from e
            if status in FATAL_STATUS:
                raise DownloadError(f"{FATAL_STATUS[status]}: {url}") from e
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
