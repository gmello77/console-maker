"""Pipeline completa con rete simulata e Docker vero (saltato se manca Docker)."""
import gzip
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from cm import pipeline
from cm.distro import Release, target_by_key
from cm.download import md5_file
from cm.roms import System

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None or subprocess.run(
        ["docker", "info"], capture_output=True).returncode != 0,
    reason="Docker non disponibile")

IMG_NAME = "batocera-h700-anbernic-rg-sp-44-20261116.img.gz"


def make_fake_base(dest: Path) -> None:
    """Immagine con il layout di Batocera: boot FAT + SHARE ext4 in fondo."""
    work = dest.parent / "fake"
    work.mkdir()
    subprocess.run(["docker", "build", "-q", "-t", pipeline.DOCKER_TAG,
                    str(pipeline.ROOT / "builder")], check=True, capture_output=True)
    subprocess.run(["docker", "run", "--rm", "--privileged", "-v", f"{work}:/work",
                    pipeline.DOCKER_TAG, "bash", "-c", """set -e
for i in 0 1 2 3; do [ -e /dev/loop$i ] || mknod -m660 /dev/loop$i b 7 $i; done
truncate -s 50M /work/fake.img
printf 'label: dos\\n2048,65536,c\\n67584,32768,83\\n' | sfdisk -q /work/fake.img
L=$(losetup -f --show -o $((67584*512)) --sizelimit $((32768*512)) /work/fake.img)
mkfs.ext4 -q -L SHARE $L; losetup -d $L"""], check=True, capture_output=True)
    dest.write_bytes(gzip.compress((work / "fake.img").read_bytes()))


def verify_share(img: Path, expect: dict[str, bytes]) -> None:
    script = """set -e
for i in 0 1 2 3; do [ -e /dev/loop$i ] || mknod -m660 /dev/loop$i b 7 $i; done
read S Z < <(parted -m -s /work/%s unit B print | awk -F: '$1=="2"{gsub("B","",$2);gsub("B","",$4);print $2,$4}')
L=$(losetup -f --show -o $S --sizelimit $Z /work/%s)
mkdir -p /m; mount -o ro $L /m; trap 'umount /m; losetup -d $L' EXIT
""" % (img.name, img.name) + "\n".join(f"cat '/m/{p}'" for p in expect)
    out = subprocess.run(["docker", "run", "--rm", "--privileged", "-v", f"{img.parent}:/work",
                          pipeline.DOCKER_TAG, "bash", "-c", script],
                         check=True, capture_output=True).stdout
    assert out == b"".join(expect.values())


@pytest.fixture
def fake_net(tmp_path, monkeypatch):
    base_gz = tmp_path / "net" / IMG_NAME
    base_gz.parent.mkdir()
    make_fake_base(base_gz)
    rel = Release(target_by_key("rg-sp"), "stable/rc", IMG_NAME,
                  "https://mirrors.example/" + IMG_NAME, "44", "20261116")
    monkeypatch.setattr(pipeline, "find_latest", lambda t: rel)
    monkeypatch.setattr(pipeline, "fetch_md5", lambda r: md5_file(base_gz))

    set_zip = tmp_path / "net" / "nes-set.zip"
    with zipfile.ZipFile(set_zip, "w") as z:
        z.writestr("Game A.nes", "AAA")
    arcade = tmp_path / "net" / "pacman.zip"
    arcade.write_bytes(b"PACZIP")

    def fake_download(url, dest, progress=None, retries=4, auth=None):
        src = {IMG_NAME: base_gz, "nes-set.zip": set_zip, "pacman.zip": arcade}[dest.name]
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dest)
        return dest

    monkeypatch.setattr(pipeline, "download", fake_download)


def settings(tmp_path, **kw):
    systems = [
        System("nes", "NES", ("https://archive.org/download/x/nes-set.zip",), True),
        System("mame", "MAME", ("https://archive.org/download/x/pacman.zip",), False),
    ]
    return pipeline.Settings(target_key="rg-sp", disk_gb=kw.pop("disk_gb", 4),
                             systems=systems, root=tmp_path / "root", **kw)


def test_full_pipeline(tmp_path, fake_net):
    logs, bars = [], []
    final = pipeline.run(settings(tmp_path, create_all_dirs=True, all_system_keys=("nes", "mame", "snes")),
                         logs.append, lambda *a: bars.append(a))
    assert final.parent == tmp_path / "root" / "out"
    assert final.name == "batocera-rg-sp-44-20261116-roms.img"
    assert any("md5 ok" in m for m in logs)
    # SHARE e' stata portata al minimo (512 MiB) e contiene le ROM al posto giusto
    assert final.stat().st_size == 67584 * 512 + 512 * 1024 * 1024
    verify_share(final, {"roms/nes/Game A.nes": b"AAA", "roms/mame/pacman.zip": b"PACZIP"})
    assert not (tmp_path / "root" / "work").exists()


def test_refuses_when_disk_too_small(tmp_path, fake_net):
    with pytest.raises(pipeline.PipelineError, match="non ci sta"):
        pipeline.run(settings(tmp_path, disk_gb=0.4), lambda m: None, lambda *a: None)
    assert not list((tmp_path / "root" / "out").glob("*.img"))


def test_bad_md5_discards_download(tmp_path, fake_net, monkeypatch):
    monkeypatch.setattr(pipeline, "fetch_md5", lambda r: "0" * 32)
    with pytest.raises(pipeline.PipelineError, match="md5"):
        pipeline.run(settings(tmp_path), lambda m: None, lambda *a: None)
    assert not (tmp_path / "root" / "cache" / "base" / IMG_NAME).exists()


def test_fit_to_disk_drops_least_important_before_downloading(tmp_path, fake_net, monkeypatch):
    """Dimensioni dichiarate enormi: il disco da 1 GB non basta per entrambi i sistemi."""
    downloaded = []
    inner = pipeline.download
    monkeypatch.setattr(pipeline, "download",
                        lambda url, dest, *a, **k: (downloaded.append(dest.name), inner(url, dest, *a, **k))[1])
    nes_url = "https://archive.org/download/x/nes-set.zip"
    mame_url = "https://archive.org/download/x/pacman.zip"
    systems = [System("nes", "NES", (nes_url,), True, priority=1),
               System("mame", "MAME", (mame_url,), False, priority=9)]   # mame piu' importante
    logs = []
    final = pipeline.run(
        pipeline.Settings(target_key="rg-sp", disk_gb=1, systems=systems, root=tmp_path / "root",
                          fit_to_disk=True, sizes={nes_url: 300_000_000, mame_url: 300_000_000}),
        logs.append, lambda *a: None)
    # budget ~ (0.97 GB - 34.6 MB - 256 MiB)/1.03 ~ 650 MB: entrambi (600 MB) ci stanno
    assert "pacman.zip" in downloaded and "nes-set.zip" in downloaded
    assert not any("tolti" in m for m in logs)
    verify_share(final, {"roms/nes/Game A.nes": b"AAA", "roms/mame/pacman.zip": b"PACZIP"})

    # stesso caso con 450 MB a testa: ne entra uno solo, quello con priorita' piu' alta
    downloaded.clear()
    logs.clear()
    final = pipeline.run(
        pipeline.Settings(target_key="rg-sp", disk_gb=1, systems=systems, root=tmp_path / "root2",
                          fit_to_disk=True, sizes={nes_url: 450_000_000, mame_url: 450_000_000}),
        logs.append, lambda *a: None)
    assert "nes-set.zip" not in downloaded and "pacman.zip" in downloaded
    assert any("[nes] tolti 1 giochi" in m for m in logs)
    verify_share(final, {"roms/mame/pacman.zip": b"PACZIP"})


def test_fit_nothing_fits_raises_before_downloading_roms(tmp_path, fake_net, monkeypatch):
    seen = []
    inner = pipeline.download
    monkeypatch.setattr(pipeline, "download",
                        lambda url, dest, *a, **k: (seen.append(dest.name), inner(url, dest, *a, **k))[1])
    url = "https://archive.org/download/x/pacman.zip"
    with pytest.raises(pipeline.PipelineError, match="nemmeno un gioco"):
        pipeline.run(
            pipeline.Settings(target_key="rg-sp", disk_gb=1,
                              systems=[System("mame", "MAME", (url,), False)],
                              root=tmp_path / "root", fit_to_disk=True, sizes={url: 5_000_000_000}),
            lambda m: None, lambda *a: None)
    assert seen == [IMG_NAME]            # solo l'immagine base, nessuna ROM scaricata
