"""Ricerca dell'ultima immagine Batocera sul mirror ufficiale.

Struttura del mirror (verificata sul sito):
    <BASE_URL>/<board>/stable/last/<file>.img.gz   (+ .md5)
    <BASE_URL>/<board>/stable/rc/<file>.img.gz     (se non c'e' una stable)
Per alcune schede (es. l'Anbernic RG SP) `last/` e' vuota e l'immagine piu'
recente sta solo in `rc/`: per questo si prova prima `last` poi `rc`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import requests

from . import USER_AGENT

BASE_URL = "https://mirrors.o2switch.fr/batocera"
CHANNELS = ("stable/last", "stable/rc")


@dataclass(frozen=True)
class Target:
    key: str
    label: str
    board: str  # cartella sul mirror


# Per aggiungere un modello basta una riga: la cartella e' quella del mirror.
TARGETS: tuple[Target, ...] = (
    Target("rg-sp", "Anbernic RG SP", "anbernic-rg-sp"),
    Target("rpi5", "Raspberry Pi 5", "bcm2712"),
    Target("rpi4", "Raspberry Pi 4 / 400", "bcm2711"),
    Target("rpi3", "Raspberry Pi 3", "bcm2837"),
    Target("pc", "PC x86_64", "x86_64"),
)


@dataclass(frozen=True)
class Release:
    target: Target
    channel: str
    filename: str
    url: str
    version: str
    date: str

    @property
    def md5_url(self) -> str:
        return self.url + ".md5"

    @property
    def is_release_candidate(self) -> bool:
        return self.channel.endswith("rc")


_IMG_RE = re.compile(r'href="(batocera-[^"/]+\.img\.gz)"')
_VER_RE = re.compile(r"-(\d+(?:\.\d+)?)-(\d{8})\.img\.gz$")


def target_by_key(key: str) -> Target:
    for t in TARGETS:
        if t.key == key:
            return t
    raise KeyError(f"target sconosciuto: {key}")


def parse_index(html: str) -> list[tuple[str, str, str]]:
    """Estrae (filename, version, date) dai link .img.gz di un indice del mirror."""
    out = []
    for name in dict.fromkeys(_IMG_RE.findall(html)):
        m = _VER_RE.search(name)
        if m:
            out.append((name, m.group(1), m.group(2)))
    return out


def find_latest(target: Target, session: requests.Session | None = None) -> Release:
    s = session or requests.Session()
    s.headers.setdefault("User-Agent", USER_AGENT)
    for channel in CHANNELS:
        url = f"{BASE_URL}/{target.board}/{channel}/"
        r = s.get(url, timeout=30)
        if r.status_code == 404:
            continue
        r.raise_for_status()
        found = parse_index(r.text)
        if found:
            name, version, date = max(found, key=lambda f: f[2])
            return Release(target, channel, name, url + name, version, date)
    raise LookupError(f"nessuna immagine trovata per {target.label} ({target.board})")


def fetch_md5(release: Release, session: requests.Session | None = None) -> str | None:
    s = session or requests.Session()
    s.headers.setdefault("User-Agent", USER_AGENT)
    r = s.get(release.md5_url, timeout=30)
    if r.status_code != 200:
        return None
    m = re.match(r"\s*([0-9a-fA-F]{32})", r.text)
    return m.group(1).lower() if m else None
