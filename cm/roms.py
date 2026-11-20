"""Lettura/validazione di roms.yaml e preparazione (staging) delle ROM."""
from __future__ import annotations

import os
import shutil
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml

from .download import filename_from_url, remote_size

# Nomi cartella Batocera piu' comuni. Una chiave fuori lista NON e' un errore:
# genera solo un avviso (Batocera ha molti sistemi e ne aggiunge di nuovi).
KNOWN_SYSTEMS = frozenset("""
3do 3ds abuse amiga1200 amiga500 amigacd32 amstradcpc apple2 atari2600 atari5200
atari7800 atari800 atarist atomiswave c64 cdi colecovision cps1 cps2 cps3 daphne
dreamcast easyrpg fbneo fds gamecube gamegear gb gba gbc gw intellivision jaguar
lutro lynx mame mastersystem megadrive model2 model3 moonlight msx1 msx2 n64 naomi
nds neogeo neogeocd nes ngp ngpc odyssey2 openbor pcengine pcenginecd pcfx pico8
pokemini ps2 psp psx saturn scummvm sega32x segacd sg1000 snes supergrafx switch
tic80 vectrex vic20 virtualboy wii wswan wswanc zx81 zxspectrum
""".split())


class RomsConfigError(Exception):
    pass


@dataclass(frozen=True)
class System:
    key: str
    name: str
    urls: tuple[str, ...]
    extract: bool = False


@dataclass
class RomsConfig:
    systems: dict[str, System]
    warnings: list[str] = field(default_factory=list)


def load_roms(path: Path) -> RomsConfig:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RomsConfigError(
            f"{path} non esiste: copia roms.example.yaml in roms.yaml e compilalo")
    except yaml.YAMLError as e:
        raise RomsConfigError(f"{path}: YAML non valido: {e}")
    return parse_roms(data)


def parse_roms(data: object) -> RomsConfig:
    if not isinstance(data, dict) or not isinstance(data.get("systems"), dict):
        raise RomsConfigError("roms.yaml: manca la sezione 'systems:'")
    cfg = RomsConfig(systems={})
    seen: dict[str, str] = {}
    for key, body in data["systems"].items():
        key = str(key)
        if not key.replace("-", "").replace("_", "").isalnum() or key != key.lower():
            raise RomsConfigError(
                f"chiave sistema non valida: {key!r} (usa il nome cartella Batocera, minuscolo)")
        body = body or {}
        if not isinstance(body, dict):
            raise RomsConfigError(f"sistema {key}: formato non valido")
        urls = body.get("urls") or []
        if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
            raise RomsConfigError(f"sistema {key}: 'urls' deve essere una lista di stringhe")
        if key not in KNOWN_SYSTEMS:
            cfg.warnings.append(f"{key}: sistema non nella lista nota, controlla il nome cartella")
        clean = []
        for u in urls:
            u = u.strip()
            p = urlparse(u)
            if p.scheme not in ("http", "https") or not p.hostname:
                raise RomsConfigError(f"sistema {key}: URL non valido: {u!r}")
            if not (p.hostname == "archive.org" or p.hostname.endswith(".archive.org")):
                cfg.warnings.append(f"{key}: {u} non e' su archive.org")
            if u in seen:
                cfg.warnings.append(f"URL duplicato in {key} (gia' in {seen[u]}): {u}")
                continue
            seen[u] = key
            clean.append(u)
        cfg.systems[key] = System(
            key=key,
            name=str(body.get("name") or key),
            urls=tuple(clean),
            extract=bool(body.get("extract", False)),
        )
    return cfg


def estimate_sizes(urls: list[str], workers: int = 8) -> dict[str, int | None]:
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return dict(zip(urls, ex.map(remote_size, urls)))


# ---------------------------------------------------------------- staging

def safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Estrae evitando path traversal (zip-slip)."""
    root = dest.resolve()
    for info in zf.infolist():
        target = (root / info.filename).resolve()
        if root != target and root not in target.parents:
            raise RomsConfigError(f"percorso sospetto nello zip: {info.filename!r}")
    zf.extractall(root)


def _link_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def stage_system(system: System, cache_dir: Path, stage_dir: Path) -> None:
    """Mette i file scaricati di `system` in stage_dir/roms/<key>/."""
    out = stage_dir / "roms" / system.key
    out.mkdir(parents=True, exist_ok=True)
    for url in system.urls:
        src = cache_dir / system.key / filename_from_url(url)
        if system.extract and src.suffix.lower() == ".zip":
            with zipfile.ZipFile(src) as zf:
                safe_extract(zf, out)
        else:
            _link_or_copy(src, out / src.name)


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
