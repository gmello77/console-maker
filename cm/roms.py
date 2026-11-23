"""Lettura/validazione di roms.yaml e preparazione (staging) delle ROM."""
from __future__ import annotations

import os
import re
import shutil
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Mapping
from urllib.parse import urlparse

import yaml

from . import archive
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
    urls: tuple[str, ...] = ()
    extract: bool = False
    item: str | None = None                   # item archive.org: tutti i suoi file
    files: tuple[str, ...] | None = None      # solo questi file dell'item
    include: str | None = None                # regex sul nome file
    exclude: str | None = None
    sizes: Mapping[str, int] = field(default_factory=dict, compare=False)  # url -> byte noti
    md5s: Mapping[str, str] = field(default_factory=dict, compare=False)   # url -> md5 pubblicato
    priority: int = 0                         # piu' alta = tenuto per primo se manca spazio
    dir: str = ""                             # solo BIOS: sottocartella di userdata/bios
    only_for: tuple[str, ...] = ()            # solo BIOS: servono se c'e' uno di questi sistemi

    @property
    def has_links(self) -> bool:
        return bool(self.urls or self.item)


@dataclass
class RomsConfig:
    systems: dict[str, System]
    warnings: list[str] = field(default_factory=list)
    bios: dict[str, System] = field(default_factory=dict)


def load_roms(path: Path) -> RomsConfig:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RomsConfigError(
            f"{path} non esiste: copia roms.example.yaml in roms.yaml e compilalo")
    except yaml.YAMLError as e:
        raise RomsConfigError(f"{path}: YAML non valido: {e}")
    return parse_roms(data)


def _check_key(key: str, what: str) -> str:
    key = str(key)
    if not key.replace("-", "").replace("_", "").isalnum() or key != key.lower():
        raise RomsConfigError(f"chiave {what} non valida: {key!r} (minuscolo, lettere e cifre)")
    return key


def _check_dir(value: object, where: str) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise RomsConfigError(f"{where}: 'dir' deve essere un testo")
    p = PurePosixPath(value)
    if "\\" in value or p.is_absolute() or ".." in p.parts:
        raise RomsConfigError(f"{where}: 'dir' deve essere una sottocartella relativa "
                              f"(niente '..', '/' iniziale o '\\'): {value!r}")
    return "" if str(p) == "." else str(p)


def _build(key: str, body: object, cfg: RomsConfig, seen: dict[str, str], kind: str) -> System:
    where = f"sistema {key}" if kind == "systems" else f"bios {key}"
    body = body or {}
    if not isinstance(body, dict):
        raise RomsConfigError(f"{where}: formato non valido")
    urls = body.get("urls") or []
    if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
        raise RomsConfigError(f"{where}: 'urls' deve essere una lista di stringhe")
    item = body.get("item")
    if item is not None and (not isinstance(item, str) or not archive.ITEM_RE.match(item)):
        raise RomsConfigError(f"{where}: 'item' non e' un identificativo archive.org: {item!r}")
    files = body.get("files")
    if files is not None and (not isinstance(files, list)
                              or not all(isinstance(f, str) for f in files)):
        raise RomsConfigError(f"{where}: 'files' deve essere una lista di nomi file")
    priority = body.get("priority", 0)
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise RomsConfigError(f"{where}: 'priority' deve essere un numero intero")
    rx = {}
    for opt in ("include", "exclude"):
        val = body.get(opt)
        if val is not None:
            try:
                re.compile(str(val))
            except re.error as e:
                raise RomsConfigError(f"{where}: '{opt}' non e' una regex valida: {e}")
            rx[opt] = str(val)
    if (files is not None or rx) and not item:
        cfg.warnings.append(f"{key}: 'files'/'include'/'exclude' valgono solo con 'item'")
    if kind == "systems" and key not in KNOWN_SYSTEMS:
        cfg.warnings.append(f"{key}: sistema non nella lista nota, controlla il nome cartella")
    only_for: tuple[str, ...] = ()
    bios_dir = ""
    if kind == "bios":
        bios_dir = _check_dir(body.get("dir"), where)
        raw = body.get("for") or []
        if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
            raise RomsConfigError(f"{where}: 'for' deve essere una lista di sistemi")
        only_for = tuple(raw)
    clean = []
    for u in urls:
        u = u.strip()
        p = urlparse(u)
        if p.scheme not in ("http", "https") or not p.hostname:
            raise RomsConfigError(f"{where}: URL non valido: {u!r}")
        if not (p.hostname == "archive.org" or p.hostname.endswith(".archive.org")):
            cfg.warnings.append(f"{key}: {u} non e' su archive.org")
        if u in seen:
            cfg.warnings.append(f"URL duplicato in {key} (gia' in {seen[u]}): {u}")
            continue
        seen[u] = key
        clean.append(u)
    return System(
        key=key, name=str(body.get("name") or key), urls=tuple(clean),
        extract=bool(body.get("extract", False)), item=item,
        files=tuple(files) if files is not None else None,
        include=rx.get("include"), exclude=rx.get("exclude"), priority=priority,
        dir=bios_dir, only_for=only_for)


def parse_roms(data: object) -> RomsConfig:
    if not isinstance(data, dict) or not isinstance(data.get("systems"), dict):
        raise RomsConfigError("roms.yaml: manca la sezione 'systems:'")
    cfg = RomsConfig(systems={})
    seen: dict[str, str] = {}
    for key, body in data["systems"].items():
        key = _check_key(key, "sistema")
        cfg.systems[key] = _build(key, body, cfg, seen, "systems")
    raw_bios = data.get("bios")
    if raw_bios is not None and not isinstance(raw_bios, dict):
        raise RomsConfigError("roms.yaml: 'bios:' deve essere un elenco di gruppi con nome")
    for key, body in (raw_bios or {}).items():
        key = _check_key(key, "bios")
        cfg.bios[key] = _build(key, body, cfg, seen, "bios")
    for b in cfg.bios.values():
        unknown = [k for k in b.only_for if k not in cfg.systems]
        if unknown:
            cfg.warnings.append(f"bios {b.key}: 'for' cita sistemi non definiti: {', '.join(unknown)}")
    return cfg


def applicable_bios(bios: list[System], system_keys: set[str]) -> list[System]:
    """I BIOS senza `for` servono sempre; gli altri solo se c'e' almeno uno dei loro sistemi."""
    return [b for b in bios if not b.only_for or set(b.only_for) & system_keys]


def expand_system(system: System, all_files: list[archive.ArchiveFile],
                  selection: list[str] | None = None,
                  logged_in: bool = False) -> tuple[System, list[str]]:
    """Trasforma `item:` in URL concreti. `selection` (dalla schermata di scelta) batte
    `files:` di roms.yaml, che batte include/exclude. Ritorna (sistema, avvisi)."""
    if not system.item:
        return system, []
    names = selection if selection is not None else system.files
    warnings = []
    if names is not None:
        missing = sorted(set(names) - {f.name for f in all_files})
        if missing:
            warnings.append(f"{system.key}: {len(missing)} file scelti non sono piu' nell'item "
                            f"{system.item} (es. {missing[0]})")
    chosen = archive.select_files(all_files, names=names, include=system.include,
                                  exclude=system.exclude)
    locked = [f for f in chosen if f.private]
    if locked and logged_in:
        warnings.append(
            f"{system.key}: {len(locked)} file dell'item {system.item} sono ad accesso riservato: "
            "uso il tuo login archive.org (se l'account non ha i permessi, il download fallira')")
    elif locked:
        warnings.append(
            f"{system.key}: {len(locked)} file dell'item {system.item} sono ad accesso riservato "
            "(serve il login di archive.org): il loro download fallira' con errore 401")
    new = {archive.file_url(system.item, f.name): f.size for f in chosen}
    md5s = {archive.file_url(system.item, f.name): f.md5.lower() for f in chosen if f.md5}
    urls = tuple(dict.fromkeys(system.urls + tuple(new)))
    return replace(system, urls=urls, sizes={**system.sizes, **new},
                   md5s={**system.md5s, **md5s}), warnings


def estimate_sizes(urls: list[str], workers: int = 8, auth=None) -> dict[str, int | None]:
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return dict(zip(urls, ex.map(lambda u: remote_size(u, auth=auth), urls)))


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


def stage_system(system: System, cache_dir: Path, stage_dir: Path, area: str = "roms") -> None:
    """Mette i file scaricati di `system` in stage_dir/<area>/...

    area "roms": stage/roms/<chiave>/;  area "bios": stage/bios/<dir>/ (dir puo' essere vuota)."""
    sub = system.key if area == "roms" else system.dir
    out = stage_dir / area / sub if sub else stage_dir / area
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
