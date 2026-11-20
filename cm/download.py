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

Progress = Callable[[int, int], None]  # (fatti, totale) in byte; totale 0 = ignoto
CHUNK = 1024 * 1024


def filename_from_url(url: str) -> str:
    name = Path(unquote(urlparse(url).path)).name
    if not name or name in (".", ".."):
        raise ValueError(f"impossibile ricavare un nome file da {url}")
    return name


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def remote_size(url: str, session: requests.Session | None = None) -> int | None:
    s = session or _session()
    try:
        r = s.head(url, allow_redirects=True, timeout=30)
        if r.status_code == 200 and "content-length" in r.headers:
            return int(r.headers["content-length"])
    except requests.RequestException:
        pass
    return None


def download(url: str, dest: Path, progress: Progress | None = None,
             retries: int = 4) -> Path:
    """Scarica `url` in `dest`; riprende da `dest.part` se esiste."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    s = _session()
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
        except (requests.RequestException, OSError) as e:
            last_err = e
            time.sleep(2 ** attempt)
    else:
        raise RuntimeError(f"download fallito dopo {retries} tentativi: {url}: {last_err}")

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
