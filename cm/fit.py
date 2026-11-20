"""Adatta la selezione di ROM allo spazio disponibile.

Ordine di riempimento: i sistemi con `priority` piu' alta per primi (a parita', l'ordine in
roms.yaml). Dentro un sistema i giochi si tengono in ordine alfabetico finche' c'e' posto;
un gioco che non ci sta viene saltato e si prova col successivo (quindi i piu' piccoli
possono entrare dopo). I file di uno stesso gioco (cue+bin, dischi multipli) non si separano.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping

from . import archive
from .download import filename_from_url
from .roms import System


@dataclass
class FitResult:
    systems: list[System]
    kept_bytes: int = 0
    dropped: dict[str, int] = field(default_factory=dict)        # sistema -> giochi tolti
    dropped_bytes: int = 0
    unknown_size_files: int = 0                                  # contati come 0 byte

    @property
    def changed(self) -> bool:
        return bool(self.dropped)


def fit_systems(systems: list[System], sizes: Mapping[str, int | None],
                budget: int) -> FitResult:
    result = FitResult(systems=[])
    remaining = max(budget, 0)
    order = sorted(range(len(systems)), key=lambda i: (-systems[i].priority, i))
    kept: dict[int, System] = {}
    for i in order:
        system = systems[i]
        by_name: dict[str, list[str]] = {}
        pseudo = []
        for url in system.urls:
            name = filename_from_url(url)
            by_name.setdefault(name, []).append(url)
            pseudo.append(archive.ArchiveFile(name, sizes.get(url) or 0))
            if sizes.get(url) is None:
                result.unknown_size_files += 1
        keep_urls: list[str] = []
        dropped_games = 0
        for game in archive.group_games(pseudo):
            urls = [u for f in game.files for u in by_name[f.name]]
            if game.size <= remaining:
                remaining -= game.size
                result.kept_bytes += game.size
                keep_urls += urls
            else:
                dropped_games += 1
                result.dropped_bytes += game.size
        if dropped_games:
            result.dropped[system.key] = dropped_games
        if keep_urls:
            kept[i] = replace(system, urls=tuple(u for u in system.urls if u in set(keep_urls)))
    result.systems = [kept[i] for i in sorted(kept)]   # nell'ordine originale
    return result
