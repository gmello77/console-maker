"""Ultime scelte del menu, salvate in config.yaml."""
from __future__ import annotations

from pathlib import Path

import yaml

DEFAULTS = {
    "device": "rg-sp",
    "disk_gb": 32,
    "systems": [],            # vuoto = tutti quelli con almeno un link
    "options": ["fit_disk", "verify_md5", "keep_cache"],
}


def load(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (FileNotFoundError, yaml.YAMLError):
        data = {}
    return {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}


def save(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


# Giochi scelti a mano per ogni sistema (schermata "Scegli giochi"): {sistema: [file]}.
# Sta in un file a parte per non riscrivere roms.yaml e perderne i commenti.
def load_selections(path: Path) -> dict[str, list[str]]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (FileNotFoundError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): [str(f) for f in v] for k, v in data.items() if isinstance(v, list)}


def save_selections(path: Path, data: dict[str, list[str]]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=True, allow_unicode=True), encoding="utf-8")
