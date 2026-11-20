"""Orchestrazione: indipendente dall'interfaccia (la TUI le passa due callback)."""
from __future__ import annotations

import platform
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .distro import Release, fetch_md5, find_latest, target_by_key
from .download import download, filename_from_url, gunzip_sparse, md5_file
from .roms import System, dir_size, stage_system

Log = Callable[[str], None]
Progress = Callable[[str, int, int], None]  # (etichetta, fatti, totale)

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


def share_size_for(rom_bytes: int) -> int:
    """Dimensione della partizione SHARE per contenere `rom_bytes` (multiplo di 1 MiB)."""
    need = int(rom_bytes * 1.03) + SHARE_MARGIN
    need = max(need, SHARE_MIN)
    return -(-need // MiB) * MiB


def partition2_start(img: Path) -> int:
    """Offset in byte della partizione 2 leggendo l'MBR."""
    with open(img, "rb") as f:
        mbr = f.read(512)
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


def _run_builder(img: Path, stage: Path, share_bytes: int, log: Log) -> None:
    builder_dir = ROOT / "builder"
    script = builder_dir / "build_image.sh"
    if shutil.which("docker"):
        log("builder: preparo l'immagine Docker")
        subprocess.run(["docker", "build", "-q", "-t", DOCKER_TAG, str(builder_dir)],
                       check=True, stdout=subprocess.DEVNULL)
        cmd = ["docker", "run", "--rm", "--privileged",
               "-v", f"{img.parent}:/work/img",
               "-v", f"{stage}:/work/stage:ro",
               DOCKER_TAG, "/build_image.sh",
               f"/work/img/{img.name}", "/work/stage", str(share_bytes)]
    elif platform.system() == "Linux":
        cmd = ["sudo", "bash", str(script), str(img), str(stage), str(share_bytes)]
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
        raise PipelineError(f"il builder e' fallito (codice {proc.returncode})")


def run(settings: Settings, log: Log, progress: Progress) -> Path:
    root = settings.root
    cache_base = root / "cache" / "base"
    cache_roms = root / "cache" / "roms"
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
    base_gz = download(rel.url, cache_base / rel.filename,
                       lambda d, t: progress("Immagine Batocera", d, t))
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

    # 2. ROM
    for system in settings.systems:
        for url in system.urls:
            log(f"[{system.key}] {filename_from_url(url)}")
            download(url, cache_roms / system.key / filename_from_url(url),
                     lambda d, t, k=system.key: progress(f"ROM {k}", d, t))
        progress(f"ROM {system.key}", 1, 1)
        stage_system(system, cache_roms, stage)
    if settings.create_all_dirs:
        for key in settings.all_system_keys:
            (stage / "roms" / key).mkdir(parents=True, exist_ok=True)

    rom_bytes = dir_size(stage)
    log(f"ROM pronte: {rom_bytes / 1e9:.2f} GB")

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

    # 4. scrittura nell'immagine
    progress("Scrittura immagine", 0, 0)
    _run_builder(img, stage, share_bytes, log)

    final = out_dir / name
    if final.exists():
        final.unlink()
    shutil.move(img, final)
    shutil.rmtree(work, ignore_errors=True)
    if not settings.keep_cache:
        shutil.rmtree(cache_roms, ignore_errors=True)
    progress("Scrittura immagine", 1, 1)
    log(f"FATTO: {final}")
    return final
