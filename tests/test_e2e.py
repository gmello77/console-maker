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

# batocera.conf predefinito dell'immagine finta (stessa forma di quello vero, in piccolo)
DEFAULT_CONF = (
    "## Hostname\nsystem.hostname=BATOCERA\nwifi.enabled=0\n#wifi.ssid=new ssid\n"
    "#wifi.key=new key\n#system.language=en_US\naudio.volume=90\nupdates.type=stable\n")
DEFAULT_CONF_SHELL = DEFAULT_CONF.replace("\n", "\\n").replace("'", "'\\''")


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
mkfs.ext4 -q -L SHARE $L; losetup -d $L
# boot FAT con boot/batocera (squashfs) che contiene il batocera.conf predefinito
B=$(losetup -f --show -o $((2048*512)) --sizelimit $((65536*512)) /work/fake.img)
mkfs.vfat $B >/dev/null
mkdir -p /s/usr/share/batocera/datainit/system /m1
printf '%b' "$DEFAULT_CONF" > /s/usr/share/batocera/datainit/system/batocera.conf
mksquashfs /s /tmp/batocera -noappend -quiet
mount -t vfat $B /m1 && mkdir -p /m1/boot && cp /tmp/batocera /m1/boot/batocera && umount /m1
losetup -d $B""".replace("$DEFAULT_CONF", DEFAULT_CONF_SHELL)], check=True, capture_output=True)
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
    bios_psx = tmp_path / "net" / "scph1001.bin"
    bios_psx.write_bytes(b"PSXBIOS")
    bios_dc = tmp_path / "net" / "dc_boot.bin"
    bios_dc.write_bytes(b"DCBIOS")

    def fake_download(url, dest, progress=None, retries=4, auth=None, connections=None):
        src = {IMG_NAME: base_gz, "nes-set.zip": set_zip, "pacman.zip": arcade,
               "scph1001.bin": bios_psx, "dc_boot.bin": bios_dc}[dest.name]
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


def _bios_groups():
    psx = System("psx_bios", "PS1 BIOS", ("https://archive.org/download/b/scph1001.bin",),
                 dir="psx", only_for=("nes",))
    anyb = System("dc_bios", "DC BIOS", ("https://archive.org/download/b/dc_boot.bin",))
    return [psx, anyb]


def test_bios_land_in_userdata_bios_and_overall_progress_is_reported(tmp_path, fake_net):
    systems = [System("nes", "NES", ("https://archive.org/download/x/nes-set.zip",), True),
               System("mame", "MAME", ("https://archive.org/download/x/pacman.zip",), False)]
    events, logs = [], []
    final = pipeline.run(
        pipeline.Settings(target_key="rg-sp", disk_gb=4, systems=systems, bios=_bios_groups(),
                          root=tmp_path / "root"),
        logs.append, lambda *a: None, lambda *e: events.append(e))
    verify_share(final, {"roms/nes/Game A.nes": b"AAA", "roms/mame/pacman.zip": b"PACZIP",
                         "bios/psx/scph1001.bin": b"PSXBIOS", "bios/dc_boot.bin": b"DCBIOS"})
    assert events[0][0] == 0 and events[0][1] == 4           # parte da 0 file su 4
    assert events[-1][:2] == (4, 4)                           # finisce 4 su 4
    done = [e[0] for e in events]
    assert done == sorted(done)                               # mai all'indietro
    assert events[-1][2] == sum(len(x) for x in (b"PACZIP", b"PSXBIOS", b"DCBIOS")) + \
        (tmp_path / "net" / "nes-set.zip").stat().st_size


def test_bios_for_a_dropped_system_is_not_downloaded(tmp_path, fake_net, monkeypatch):
    downloaded = []
    inner = pipeline.download
    monkeypatch.setattr(pipeline, "download",
                        lambda url, dest, *a, **k: (downloaded.append(dest.name), inner(url, dest, *a, **k))[1])
    nes_url = "https://archive.org/download/x/nes-set.zip"
    mame_url = "https://archive.org/download/x/pacman.zip"
    systems = [System("nes", "NES", (nes_url,), True, priority=1),
               System("mame", "MAME", (mame_url,), False, priority=9)]
    logs = []
    final = pipeline.run(
        pipeline.Settings(target_key="rg-sp", disk_gb=1, systems=systems, bios=_bios_groups(),
                          root=tmp_path / "root", fit_to_disk=True,
                          sizes={nes_url: 450_000_000, mame_url: 450_000_000,
                                 "https://archive.org/download/b/scph1001.bin": 1000,
                                 "https://archive.org/download/b/dc_boot.bin": 1000}),
        logs.append, lambda *a: None)
    assert "nes-set.zip" not in downloaded                   # tolto dall'adattamento
    assert "scph1001.bin" not in downloaded                  # il suo BIOS dipendeva da nes
    assert "dc_boot.bin" in downloaded                       # quello senza 'for' resta
    assert any("BIOS non necessari" in m and "psx_bios" in m for m in logs)
    verify_share(final, {"roms/mame/pacman.zip": b"PACZIP", "bios/dc_boot.bin": b"DCBIOS"})


# ---------------------------------------------------------------- preconfigurazione (device.yaml)
from cm.device import merge_conf, parse_device


def share_listing(img: Path, rel: str) -> str:
    """Contenuto (ls) di una cartella della SHARE, o 'MANCA'."""
    script = """set -e
for i in 0 1 2 3; do [ -e /dev/loop$i ] || mknod -m660 /dev/loop$i b 7 $i; done
read S Z < <(parted -m -s /work/%s unit B print | awk -F: '$1=="2"{gsub("B","",$2);gsub("B","",$4);print $2,$4}')
L=$(losetup -f --show -r -o $S --sizelimit $Z /work/%s)
mkdir -p /m; mount -o ro $L /m; trap 'umount /m; losetup -d $L' EXIT
if [ -e '/m/%s' ]; then ls -A '/m/%s'; else echo MANCA; fi""" % (img.name, img.name, rel, rel)
    return subprocess.run(["docker", "run", "--rm", "--privileged", "-v", f"{img.parent}:/work",
                           pipeline.DOCKER_TAG, "bash", "-c", script],
                          check=True, capture_output=True, text=True).stdout.strip()


DEVICE = {"language": "it_IT", "timezone": "Europe/Rome",
          "wifi": {"country": "IT", "networks": [{"ssid": "CasaMia", "key": "pa#ss;wo$rd-segreta"}]},
          "screenscraper": {"user": "mario", "password": "scraper-segreta"}}
SECRETS = ("pa#ss;wo$rd-segreta", "pa\\#ss", "scraper-segreta")


def one_system():
    return [System("nes", "NES", ("https://archive.org/download/x/nes-set.zip",), True)]


def test_preconfiguration_is_merged_into_the_images_own_defaults(tmp_path, fake_net):
    device = parse_device(DEVICE)
    logs = []
    final = pipeline.run(
        pipeline.Settings(target_key="rg-sp", disk_gb=4, systems=one_system(),
                          root=tmp_path / "root", device=device),
        logs.append, lambda *a: None)
    expected = merge_conf(DEFAULT_CONF, device.conf_overrides()).encode()
    es = device.es_settings_xml().encode()
    verify_share(final, {"system/batocera.conf": expected,
                         "system/configs/emulationstation/es_settings.cfg": es,
                         "roms/nes/Game A.nes": b"AAA"})
    text = expected.decode()
    assert "audio.volume=90" in text and "updates.type=stable" in text      # i predefiniti restano
    assert "system.language=it_IT" in text and "wifi.enabled=1" in text
    assert "wifi.key=pa\\#ss\\;wo\\$rd-segreta" in text                       # caratteri protetti
    assert "#wifi.ssid" not in text and "wifi.ssid=CasaMia" in text
    # nei log solo il riassunto: nessun segreto
    joined = "\n".join(logs)
    assert "preconfigurazione applicata: lingua it_IT" in joined
    for secret in SECRETS + ("mario",):
        assert secret not in joined


def test_without_device_nothing_is_written_to_userdata_system(tmp_path, fake_net):
    final = pipeline.run(
        pipeline.Settings(target_key="rg-sp", disk_gb=4, systems=one_system(),
                          root=tmp_path / "root"),
        lambda m: None, lambda *a: None)
    assert share_listing(final, "system") == "MANCA"      # Batocera creera' i suoi predefiniti
    empty = parse_device({})
    final2 = pipeline.run(
        pipeline.Settings(target_key="rg-sp", disk_gb=4, systems=one_system(),
                          root=tmp_path / "root2", device=empty),
        lambda m: None, lambda *a: None)
    assert share_listing(final2, "system") == "MANCA"      # configurazione vuota = niente da fare


def test_only_language_does_not_create_es_settings(tmp_path, fake_net):
    final = pipeline.run(
        pipeline.Settings(target_key="rg-sp", disk_gb=4, systems=one_system(),
                          root=tmp_path / "root", device=parse_device({"language": "it_IT"})),
        lambda m: None, lambda *a: None)
    assert share_listing(final, "system") == "batocera.conf"


def test_missing_default_conf_aborts_instead_of_writing_an_incomplete_file(tmp_path, fake_net, monkeypatch):
    monkeypatch.setattr(pipeline, "_run_extract", lambda *a, **k: None)   # nessun file estratto
    with pytest.raises(pipeline.PipelineError, match="batocera.conf predefinito"):
        pipeline.run(
            pipeline.Settings(target_key="rg-sp", disk_gb=4, systems=one_system(),
                              root=tmp_path / "root", device=parse_device(DEVICE)),
            lambda m: None, lambda *a: None)
    assert not list((tmp_path / "root" / "out").glob("*.img"))


def test_secrets_are_removed_from_disk_even_if_the_builder_fails(tmp_path, fake_net, monkeypatch):
    def boom(*a, **k):
        raise pipeline.PipelineError("simulato")
    monkeypatch.setattr(pipeline, "_run_builder", boom)
    with pytest.raises(pipeline.PipelineError, match="simulato"):
        pipeline.run(
            pipeline.Settings(target_key="rg-sp", disk_gb=4, systems=one_system(),
                              root=tmp_path / "root", device=parse_device(DEVICE)),
            lambda m: None, lambda *a: None)
    leftovers = [p for p in (tmp_path / "root").rglob("*")
                 if p.is_file() and any(s.encode() in p.read_bytes() for s in ("CasaMia", "scraper-segreta"))]
    assert leftovers == []                                 # nessun file con Wi-Fi o password su disco
