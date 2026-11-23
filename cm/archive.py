"""Elenco dei file di un item archive.org e raggruppamento in giochi.

L'API `https://archive.org/metadata/<item>` restituisce `{}` (non un 404) per un item
inesistente, e tra i file `original` include anche metadati e miniature: vanno scartati.
"""
from __future__ import annotations

import json
import posixpath
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import requests

from . import USER_AGENT

META_URL = "https://archive.org/metadata/{item}"
DOWNLOAD_URL = "https://archive.org/download/{item}/{name}"
CACHE_TTL = 24 * 3600
ITEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SKIP_FORMATS = {"Metadata", "Item Tile"}


@dataclass(frozen=True)
class ArchiveFile:
    name: str
    size: int
    md5: str | None = None
    private: bool = False   # richiede il login di archive.org


@dataclass(frozen=True)
class Game:
    """Un gioco = tutti i file che gli appartengono (cue+bin, dischi multipli, m3u)."""
    title: str
    files: tuple[ArchiveFile, ...]

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files)


def file_url(item: str, name: str) -> str:
    return DOWNLOAD_URL.format(item=item, name=quote(name))


def parse_download_url(url: str) -> tuple[str, str] | None:
    """(item, nome file) per gli URL del tipo https://archive.org/download/<item>/<file>.
    Altre forme (mirror, server dei dati) non si riconoscono: None."""
    p = urlparse(url)
    if p.hostname not in ("archive.org", "www.archive.org"):
        return None
    parts = p.path.split("/", 3)                  # ['', 'download', item, 'file/anche/in/sottocartelle']
    if len(parts) != 4 or parts[1] != "download" or not parts[2] or not parts[3]:
        return None
    item, name = unquote(parts[2]), unquote(parts[3])
    return (item, name) if ITEM_RE.match(item) else None


def parse_files(data: dict) -> list[ArchiveFile]:
    out = []
    for f in data.get("files", []):
        if f.get("source") != "original" or f.get("format") in _SKIP_FORMATS:
            continue
        name = f.get("name") or ""
        if not name or name.endswith(".torrent"):
            continue
        out.append(ArchiveFile(name, int(f.get("size") or 0), f.get("md5"),
                               str(f.get("private", "")).lower() == "true"))
    return out


def list_files(item: str, cache_dir: Path | None = None,
               session: requests.Session | None = None) -> list[ArchiveFile]:
    if not ITEM_RE.match(item):
        raise ValueError(f"identificativo item non valido: {item!r}")
    cache = cache_dir / f"{item}.json" if cache_dir else None
    if cache and cache.exists() and time.time() - cache.stat().st_mtime < CACHE_TTL:
        return parse_files(json.loads(cache.read_text(encoding="utf-8")))
    s = session or requests.Session()
    s.headers.setdefault("User-Agent", USER_AGENT)
    r = s.get(META_URL.format(item=item), timeout=60)
    r.raise_for_status()
    data = r.json()
    if not data or "files" not in data:
        raise LookupError(f"item archive.org non trovato o non accessibile: {item}")
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data), encoding="utf-8")
    return parse_files(data)


def select_files(files: list[ArchiveFile], *, names: list[str] | tuple[str, ...] | None = None,
                 include: str | None = None, exclude: str | None = None) -> list[ArchiveFile]:
    """`names` (scelta esplicita) ha la precedenza su include/exclude (regex, senza maiuscole)."""
    if names is not None:
        wanted = set(names)
        return [f for f in files if f.name in wanted]
    inc = re.compile(include, re.I) if include else None
    exc = re.compile(exclude, re.I) if exclude else None
    return [f for f in files
            if (inc is None or inc.search(f.name)) and not (exc and exc.search(f.name))]


_PART_RE = re.compile(r"\s*\((?:Dis[ck]|CD|Track)\s*\d+(?:\s*of\s*\d+)?\)", re.I)


def group_games(files: list[ArchiveFile]) -> list[Game]:
    """Raggruppa per titolo, ignorando estensione e '(Disc N)' / '(Track N)'."""
    groups: dict[str, list[ArchiveFile]] = {}
    for f in files:
        base = posixpath.splitext(f.name)[0]
        title = _PART_RE.sub("", base).strip() or base
        groups.setdefault(title, []).append(f)
    return [Game(t, tuple(fs)) for t, fs in sorted(groups.items(), key=lambda kv: kv[0].lower())]
