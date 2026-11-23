"""Orchestrazione: indipendente dall'interfaccia (la TUI le passa due callback)."""
from __future__ import annotations

import gzip
import os
import platform
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from .auth import ArchiveAuth
from .device import DeviceConfig, merge_conf, merge_es_settings
from . import archive
from .distro import Release, fetch_md5, find_latest, target_by_key
from .download import (DEFAULT_CONNECTIONS, download, filename_from_url, gunzip_sparse,
                       md5_file, remote_size)
from .fit import fit_systems
from .roms import System, applicable_bios, dir_size, stage_system

Log = Callable[[str], None]
Progress = Callable[[str, int, int], None]  # (etichetta, fatti, totale)
# (file fatti, file totali, byte fatti, byte totali) per ROM e BIOS
Overall = Callable[[int, int, int, int], None]

ROOT = Path(__file__).resolve().parent.parent
MiB = 1024 * 1024
SHARE_MIN = 512 * MiB          # dimensione originale di SHARE nell'immagine
SHARE_MARGIN = 256 * MiB       # spazio libero lasciato dopo le ROM
DOCKER_TAG = "console-maker-builder"


class PipelineError(Exception):
    pass


@dataclass
class Settings:
    target_key: str
    disk_gb: float
    systems: list[System]
    verify_md5: bool = True
    keep_cache: bool = True
    create_all_dirs: bool = False
    all_system_keys: tuple[str, ...] = ()
    root: Path = ROOT
    fit_to_disk: bool = False      # se non ci stanno, toglie i giochi meno importanti
    sizes: Mapping[str, int | None] = field(default_factory=dict)   # url -> byte (se noti)
    bios: list[System] = field(default_factory=list)   # gruppi BIOS di roms.yaml (gia' espansi)
    device: DeviceConfig | None = None   # lingua, Wi-Fi, ScreenScraper... (device.yaml)
    connections: int = DEFAULT_CONNECTIONS   # connessioni parallele per file
    auth: ArchiveAuth | None = None   # login archive.org: usato solo per le ROM, mai per Batocera


def share_size_for(rom_bytes: int) -> int:
    """Dimensione della partizione SHARE per contenere `rom_bytes` (multiplo di 1 MiB)."""
    need = int(rom_bytes * 1.03) + SHARE_MARGIN
    need = max(need, SHARE_MIN)
    return -(-need // MiB) * MiB


def partition2_start(img: Path) -> int:
    """Offset in byte della partizione 2 leggendo l'MBR."""
    with open(img, "rb") as f:
        return _p2_start(f.read(512))


def partition2_start_gz(img_gz: Path) -> int:
    """Come partition2_start ma sull'immagine compressa: serve solo l'inizio del flusso."""
    with gzip.open(img_gz, "rb") as f:
        return _p2_start(f.read(512))


def max_rom_bytes(disk_gb: float, p2_start: int) -> int:
    """Quanti byte di ROM ci stanno: inverso di share_size_for + margine, sul disco indicato."""
    return max(0, int((disk_capacity_bytes(disk_gb) - p2_start - SHARE_MARGIN) / 1.03))


def _p2_start(mbr: bytes) -> int:
    if mbr[510:512] != b"\x55\xaa":
        raise PipelineError("l'immagine non ha una tabella MBR: layout inatteso")
    entry = mbr[446 + 16 : 446 + 32]  # seconda voce
    typ = entry[4]
    start, _ = struct.unpack("<II", entry[8:16])
    if typ == 0:
        raise PipelineError("la partizione 2 (SHARE) non esiste nell'immagine")
    return start * 512


def disk_capacity_bytes(disk_gb: float) -> int:
    # Le schede sono vendute in GB decimali e ne restano ~97% utilizzabili.
    return int(disk_gb * 1_000_000_000 * 0.97)


def fetch_release(settings: Settings) -> Release:
    return find_latest(target_by_key(settings.target_key))


def _exec_builder(docker_args: list[str], linux_cmd: list[str], log: Log, what: str) -> None:
    """Esegue uno script del builder: in Docker (macOS) oppure direttamente su Linux."""
    builder_dir = ROOT / "builder"
    if shutil.which("docker"):
        log("builder: preparo l'immagine Docker")
        subprocess.run(["docker", "build", "-q", "-t", DOCKER_TAG, str(builder_dir)],
                       check=True, stdout=subprocess.DEVNULL)
        cmd = ["docker", "run", "--rm", "--privileged", *docker_args]
    elif platform.system() == "Linux":
        cmd = linux_cmd
    else:
        raise PipelineError("serve Docker (su macOS/Windows) per modificare l'immagine")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    assert proc.stdout
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log(f"builder: {line}")
    if proc.wait() != 0:
        raise PipelineError(f"{what} e' fallito (codice {proc.returncode})")


def _run_builder(img: Path, stage: Path, share_bytes: int, log: Log) -> None:
    script = ROOT / "builder" / "build_image.sh"
    _exec_builder(
        ["-v", f"{img.parent}:/work/img", "-v", f"{stage}:/work/stage:ro",
         DOCKER_TAG, "/build_image.sh", f"/work/img/{img.name}", "/work/stage", str(share_bytes)],
        ["sudo", "bash", str(script), str(img), str(stage), str(share_bytes)],
        log, "il builder")


def _run_extract(img: Path, out: Path, log: Log) -> None:
    """Estrae da `img` (sola lettura) i file di configurazione predefiniti in `out`."""
    script = ROOT / "builder" / "extract_defaults.sh"
    _exec_builder(
        ["-v", f"{img.parent}:/work/img:ro", "-v", f"{out}:/work/out",
         DOCKER_TAG, "/extract_defaults.sh", f"/work/img/{img.name}", "/work/out"],
        ["sudo", "bash", str(script), str(img), str(out)],
        log, "l'estrazione dei file predefiniti")


STAGED_DEVICE_FILES = ("system/batocera.conf", "system/configs/emulationstation/es_settings.cfg")


def preconfigure(device: DeviceConfig, img: Path, stage: Path, work: Path, log: Log) -> None:
    """Scrive in stage/ batocera.conf (e, se serve, es_settings.cfg) fusi con i predefiniti
    estratti dall'immagine stessa. Nei log finisce solo un riassunto, mai i valori."""
    defaults = work / "defaults"
    defaults.mkdir(parents=True, exist_ok=True)
    _run_extract(img, defaults, log)
    default_conf = defaults / "batocera.conf"
    if not default_conf.is_file():
        raise PipelineError("non trovo il batocera.conf predefinito nell'immagine: non applico "
                            "la preconfigurazione per non scrivere un file incompleto")
    out = stage / "system"
    out.mkdir(parents=True, exist_ok=True)
    (out / "batocera.conf").write_text(
        merge_conf(default_conf.read_text(encoding="utf-8"), device.conf_overrides()),
        encoding="utf-8")
    xml = device.es_settings_xml()
    if xml:
        es_dir = out / "configs" / "emulationstation"
        es_dir.mkdir(parents=True, exist_ok=True)
        default_es = defaults / "es_settings.cfg"
        if default_es.is_file():
            xml = merge_es_settings(default_es.read_text(encoding="utf-8"), xml)
        (es_dir / "es_settings.cfg").write_text(xml, encoding="utf-8")
    log(f"preconfigurazione applicata: {device.summary()}")


# ---------------------------------------------------------------- spazio sul disco del Mac
# Occupazione dell'immagine decompressa (file sparso) per byte del .gz. Misurato con
# st_blocks sull'immagine vera dell'RG SP: 2,54 GB occupati per 2,35 GB di .gz (1,08).
# Si tiene un margine del 10% perche' per gli altri dispositivi non e' misurato.
ALLOC_FACTOR = 1.2
EXTRACT_FACTOR = 2.0          # stima: uno zip di ROM scompattato pesa circa il doppio
SPACE_MARGIN = 1_000_000_000  # spazio che deve restare libero a lavoro finito


@dataclass(frozen=True)
class SpaceNeed:
    downloads: int            # byte ancora da scaricare in cache/
    image: int                # immagine decompressa + ROM copiate dentro
    extract: int = 0          # ROM scompattate (copia in work/ e in piu' nell'immagine)
    stage_copy: int = 0       # copie al posto degli hard link (se cache e work sono su volumi diversi)
    margin: int = SPACE_MARGIN

    @property
    def total(self) -> int:
        return self.downloads + self.image + self.extract + self.stage_copy + self.margin


def space_needed(*, base_gz_size: int, base_gz_missing: int, roms_missing: int, roms_total: int,
                 extract_zip_bytes: int = 0, hardlinks: bool = True) -> SpaceNeed:
    """Stima dello spazio che serve sul volume di lavoro. E' una stima prudente, non un limite
    esatto: la dimensione reale dell'immagine decompressa si conosce solo dopo averla fatta."""
    return SpaceNeed(
        downloads=base_gz_missing + roms_missing,
        image=int(base_gz_size * ALLOC_FACTOR) + int(roms_total * 1.02),
        extract=int(extract_zip_bytes * (2 * EXTRACT_FACTOR - 1)),
        stage_copy=0 if hardlinks else max(roms_total - extract_zip_bytes, 0))


def allocated_bytes(path: Path) -> int:
    """Byte realmente occupati su disco (un file sparso occupa meno della sua dimensione)."""
    try:
        if path.is_file():
            return path.stat().st_blocks * 512
        return sum(f.stat().st_blocks * 512 for f in path.rglob("*") if f.is_file())
    except OSError:
        return 0


def check_space(need: SpaceNeed, root: Path) -> int:
    """Ferma la pipeline se sul volume di `root` non c'e' abbastanza spazio. Ritorna lo spazio libero."""
    free = shutil.disk_usage(root).free
    if free >= need.total:
        return free
    parts = [f"download {need.downloads / 1e9:.1f} GB", f"immagine {need.image / 1e9:.1f} GB"]
    if need.extract:
        parts.append(f"ROM scompattate {need.extract / 1e9:.1f} GB")
    if need.stage_copy:
        parts.append(f"copie temporanee {need.stage_copy / 1e9:.1f} GB")
    parts.append(f"margine {need.margin / 1e9:.1f} GB")
    cache, out = allocated_bytes(root / "cache"), allocated_bytes(root / "out")
    raise PipelineError(
        f"spazio insufficiente sul disco del Mac ({root}): servono circa {need.total / 1e9:.1f} GB "
        f"({', '.join(parts)}) e ne restano liberi {free / 1e9:.1f}. "
        f"Libera spazio: cache/ occupa {cache / 1e9:.1f} GB (si puo' cancellare, si riscarica), "
        f"out/ {out / 1e9:.1f} GB (immagini gia' create).")


class _Md5Index:
    """md5 pubblicati da archive.org per gli URL scaricati, con un solo accesso per item."""

    def __init__(self, cache_dir: Path, log: Log) -> None:
        self.cache_dir, self.log = cache_dir, log
        self._items: dict[str, dict[str, str]] = {}

    def expected(self, system: System, url: str) -> str | None:
        if url in system.md5s:                       # gia' noto dall'elenco dell'item
            return system.md5s[url]
        ref = archive.parse_download_url(url)
        if ref is None:
            return None
        item, name = ref
        if item not in self._items:
            try:
                files = archive.list_files(item, self.cache_dir)
                self._items[item] = {f.name: f.md5.lower() for f in files if f.md5}
            except Exception as e:                   # rete, item riservato...: non si verifica
                self.log(f"md5 non disponibili per l'item {item}: {e}")
                self._items[item] = {}
        return self._items[item].get(name)


def _verified_download(settings: "Settings", system: System, url: str, dest_path: Path,
                       index: _Md5Index, on_progress: Callable[[int, int], None],
                       progress: Progress, log: Log) -> tuple[Path, bool]:
    """Scarica `url` e, se possibile, ne verifica l'md5. Ritorna (file, verificato).

    Un file sbagliato (anche gia' in cache) si scarta e si riscarica una volta; se resta
    sbagliato la pipeline si ferma: meglio che mettere una ROM corrotta nell'immagine."""
    def fetch() -> Path:
        return download(url, dest_path, on_progress, auth=settings.auth,
                        connections=settings.connections)

    dest = fetch()
    if not settings.verify_md5:
        return dest, False
    want = index.expected(system, url)
    if want is None:
        return dest, False
    name = dest.name
    for attempt in (1, 2):
        got = md5_file(dest, lambda d, t: progress(f"Verifica md5: {name}", d, t))
        if got == want:
            return dest, True
        dest.unlink(missing_ok=True)
        if attempt == 2:
            break
        log(f"md5 diverso per {name} (cache corrotta o download danneggiato): riscarico")
        dest = fetch()
    raise PipelineError(
        f"md5 errato per {name} anche dopo un secondo download: il file su archive.org e' "
        f"cambiato o la connessione lo danneggia. Atteso {want}, ottenuto {got}.")


class _Overall:
    """Avanzamento complessivo dei download di ROM e BIOS."""

    def __init__(self, files_total: int, bytes_total: int, cb: Overall | None) -> None:
        self.files_total, self.bytes_total, self.cb = files_total, bytes_total, cb
        self.files_done = 0
        self.bytes_done = 0

    def _emit(self, partial: int = 0) -> None:
        if self.cb:
            self.cb(self.files_done, self.files_total, self.bytes_done + partial, self.bytes_total)

    start = _emit

    def partial(self, done: int) -> None:
        self._emit(done)

    def finished(self, dest: Path) -> None:
        self.files_done += 1
        self.bytes_done += dest.stat().st_size
        self._emit()


def run(settings: Settings, log: Log, progress: Progress, overall: Overall | None = None) -> Path:
    root = settings.root
    cache_base = root / "cache" / "base"
    cache_roms = root / "cache" / "roms"
    cache_bios = root / "cache" / "bios"
    work = root / "work"
    stage = work / "stage"
    out_dir = root / "out"
    for d in (cache_base, cache_roms, out_dir):
        d.mkdir(parents=True, exist_ok=True)
    # work/ e' sempre ricreata: contiene solo file derivati dalla cache.
    shutil.rmtree(work, ignore_errors=True)
    stage.mkdir(parents=True)

    # 1. immagine base
    rel = fetch_release(settings)
    kind = "release candidate" if rel.is_release_candidate else "stable"
    log(f"Batocera {rel.version} ({kind}, {rel.date}) per {rel.target.label}")
    # controllo rapido: c'e' almeno posto per l'immagine base e per la sua versione decompressa?
    base_size = remote_size(rel.url)
    if base_size:
        base_dest = cache_base / rel.filename
        have = base_dest.exists() and base_dest.stat().st_size == base_size
        check_space(space_needed(base_gz_size=base_size, base_gz_missing=0 if have else base_size,
                                 roms_missing=0, roms_total=0), root)
    base_gz = download(rel.url, cache_base / rel.filename,
                       lambda d, t: progress("Immagine Batocera", d, t),
                       connections=settings.connections)
    if settings.verify_md5:
        expected = fetch_md5(rel)
        if expected is None:
            log("md5 non pubblicato sul mirror: verifica saltata")
        else:
            got = md5_file(base_gz, lambda d, t: progress("Verifica md5", d, t))
            if got != expected:
                base_gz.unlink()
                raise PipelineError("md5 dell'immagine errato: file scartato, riprova")
            log("md5 ok")

    # 1b. se serve, adatta la selezione al disco (prima di scaricare ROM che poi non entrano)
    systems = settings.systems
    sizes = dict(settings.sizes)
    if settings.fit_to_disk:
        bios_pre = applicable_bios(settings.bios, {s.key for s in systems})
        for url in (u for s in [*systems, *bios_pre] for u in s.urls):
            if sizes.get(url) is None:
                sizes[url] = remote_size(url, auth=settings.auth)
        # i BIOS sono piccoli e servono: si riserva il loro spazio prima di sfoltire le ROM
        bios_bytes = sum(sizes.get(u) or 0 for b in bios_pre for u in b.urls)
        if bios_bytes:
            log(f"BIOS: riservo {bios_bytes / 1e6:.0f} MB")
        budget = max_rom_bytes(settings.disk_gb, partition2_start_gz(base_gz)) - bios_bytes
        fit = fit_systems(systems, sizes, max(budget, 0))
        if fit.changed:
            log(f"su {settings.disk_gb:g} GB non entra tutto: tengo {fit.kept_bytes / 1e9:.2f} GB "
                f"di ROM, tolgo {fit.dropped_bytes / 1e9:.2f} GB")
            for key, n in fit.dropped.items():
                log(f"  [{key}] tolti {n} giochi")
        if fit.unknown_size_files:
            log(f"attenzione: {fit.unknown_size_files} file di dimensione ignota non sono "
                "stati contati")
        if not fit.systems:
            raise PipelineError("nemmeno un gioco ci sta nel disco indicato: scegli un disco "
                                "piu' grande")
        systems = fit.systems

    # 2. ROM e BIOS (i BIOS solo se resta almeno uno dei sistemi per cui servono)
    bios = applicable_bios(settings.bios, {s.key for s in systems})
    skipped = [b.key for b in settings.bios if b not in bios]
    if skipped:
        log(f"BIOS non necessari (nessun sistema collegato nell'immagine): {', '.join(skipped)}")
    groups = [("ROM", s, cache_roms, "roms") for s in systems] + \
             [("BIOS", b, cache_bios, "bios") for b in bios]
    plan = [(s, u) for _, s, _, _ in groups for u in s.urls]
    total = sum(sizes.get(u) or s.sizes.get(u) or 0 for s, u in plan)

    # controllo completo dello spazio, ora che si sa quali file entrano e cosa e' gia' in cache
    missing = known_total = zip_bytes = unknown_sizes = 0
    for kind, system, cache_root, area in groups:
        for url in system.urls:
            size = sizes.get(url) or system.sizes.get(url)
            if not size:
                unknown_sizes += 1
                continue
            known_total += size
            dest = cache_root / system.key / filename_from_url(url)
            if not (dest.exists() and dest.stat().st_size == size):
                part = dest.with_name(dest.name + ".part")   # il parziale gia' scaricato non si ricontano
                missing += max(size - (allocated_bytes(part) if part.exists() else 0), 0)
            if system.extract and url.lower().endswith(".zip"):
                zip_bytes += size
    need = space_needed(base_gz_size=base_gz.stat().st_size, base_gz_missing=0,
                        roms_missing=missing, roms_total=known_total, extract_zip_bytes=zip_bytes,
                        hardlinks=os.stat(cache_roms).st_dev == os.stat(stage).st_dev)
    free = check_space(need, root)
    log(f"spazio sul Mac: servono circa {need.total / 1e9:.1f} GB, liberi {free / 1e9:.1f} GB")
    if unknown_sizes:
        log(f"attenzione: {unknown_sizes} file di dimensione ignota non sono contati nel calcolo dello spazio")
    tracker = _Overall(len(plan), total, overall)
    tracker.start()
    md5_index = _Md5Index(root / "cache" / "meta", log)
    checked = unchecked = 0
    for kind, system, cache_root, area in groups:
        for url in system.urls:
            name = filename_from_url(url)
            log(f"[{kind} {system.key}] {name}")
            label = f"{kind} {system.key}: {name}"

            def on_progress(d: int, t: int, label: str = label) -> None:
                progress(label, d, t)
                tracker.partial(d)

            dest, ok = _verified_download(settings, system, url, cache_root / system.key / name,
                                          md5_index, on_progress, progress, log)
            checked += ok
            unchecked += not ok
            tracker.finished(dest)
        stage_system(system, cache_root, stage, area=area)
    if settings.verify_md5 and plan:
        log(f"md5: {checked} file verificati" +
            (f", {unchecked} senza md5 pubblicato (non verificabili)" if unchecked else ""))
    if settings.create_all_dirs:
        for key in settings.all_system_keys:
            (stage / "roms" / key).mkdir(parents=True, exist_ok=True)

    rom_bytes = dir_size(stage)
    log(f"ROM e BIOS pronti: {rom_bytes / 1e9:.2f} GB")

    # 3. immagine di lavoro
    name = f"batocera-{rel.target.key}-{rel.version}-{rel.date}-roms.img"
    img = work / name
    log("decomprimo l'immagine base")
    gunzip_sparse(base_gz, img, lambda d, t: progress("Decompressione", d, t))
    share_bytes = share_size_for(rom_bytes)
    final_size = partition2_start(img) + share_bytes
    capacity = disk_capacity_bytes(settings.disk_gb)
    log(f"immagine finale: {final_size / 1e9:.2f} GB su {settings.disk_gb:g} GB di disco")
    if final_size > capacity:
        raise PipelineError(
            f"non ci sta: l'immagine finale e' {final_size / 1e9:.1f} GB, "
            f"il disco ne offre circa {capacity / 1e9:.1f}. "
            "Togli qualche sistema o scegli un disco piu' grande.")

    # 3b. preconfigurazione (lingua, Wi-Fi, ScreenScraper)
    if settings.device and not settings.device.is_empty:
        preconfigure(settings.device, img, stage, work, log)

    # 4. scrittura nell'immagine
    progress("Scrittura immagine", 0, 0)
    try:
        _run_builder(img, stage, share_bytes, log)
    finally:
        # i file con Wi-Fi e password non devono restare su disco, nemmeno se il builder fallisce
        for rel in STAGED_DEVICE_FILES:
            (stage / rel).unlink(missing_ok=True)

    final = out_dir / name
    if final.exists():
        final.unlink()
    shutil.move(img, final)
    shutil.rmtree(work, ignore_errors=True)
    if not settings.keep_cache:
        shutil.rmtree(cache_roms, ignore_errors=True)
        shutil.rmtree(cache_bios, ignore_errors=True)
    progress("Scrittura immagine", 1, 1)
    log(f"FATTO: {final}")
    return final
