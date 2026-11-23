"""Pipeline completa con rete simulata e Docker vero (saltato se manca Docker)."""
import gzip
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from cm import archive, pipeline
from cm.archive import ArchiveFile
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


class FakeNet:
    """Rete finta: file buoni, md5 pubblicati per item e download corrotti a comando."""

    def __init__(self, files):
        self.files = files                  # nome -> file buono
        self.meta = {}                      # item -> [ArchiveFile] (con md5)
        self.corrupt_once = set()           # il primo download di questi file e' danneggiato
        self.corrupt_always = set()
        self.served = {}                    # nome -> quante volte e' stato scaricato
        self.list_calls = []                # item di cui si sono chiesti i metadati
        self.remote_sizes = {}              # url -> dimensione dichiarata (HEAD)


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

    net = FakeNet({IMG_NAME: base_gz, "nes-set.zip": set_zip, "pacman.zip": arcade,
                   "scph1001.bin": bios_psx, "dc_boot.bin": bios_dc})
    for item, names in (("x", ("nes-set.zip", "pacman.zip")), ("b", ("scph1001.bin", "dc_boot.bin"))):
        net.meta[item] = [ArchiveFile(n, net.files[n].stat().st_size, md5_file(net.files[n]))
                          for n in names]

    def fake_download(url, dest, progress=None, retries=4, auth=None, connections=None):
        name = dest.name
        src = net.files[name]
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.stat().st_size == src.stat().st_size:
            return dest                      # come il vero download(): un file completo in cache si riusa
        n = net.served[name] = net.served.get(name, 0) + 1
        if name in net.corrupt_always or (name in net.corrupt_once and n == 1):
            dest.write_bytes(b"X" * src.stat().st_size)      # stessa dimensione, contenuto sbagliato
        else:
            shutil.copy(src, dest)
        return dest

    def fake_list_files(item, cache_dir=None, session=None):
        net.list_calls.append(item)
        return net.meta.get(item, [])

    net.remote_sizes[rel.url] = base_gz.stat().st_size
    monkeypatch.setattr(pipeline, "download", fake_download)
    monkeypatch.setattr(archive, "list_files", fake_list_files)     # mai la rete vera nei test
    monkeypatch.setattr(pipeline, "remote_size", lambda url, session=None, auth=None: net.remote_sizes.get(url))
    net.rel = rel
    return net


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


# ---------------------------------------------------------------- verifica md5 delle ROM e dei BIOS
def md5_settings(tmp_path, **kw):
    systems = [System("nes", "NES", ("https://archive.org/download/x/nes-set.zip",), True),
               System("mame", "MAME", ("https://archive.org/download/x/pacman.zip",), False)]
    bios = [System("dc", "DC", ("https://archive.org/download/b/dc_boot.bin",))]
    return pipeline.Settings(target_key="rg-sp", disk_gb=4, systems=systems, bios=bios,
                             root=tmp_path / "root", **kw)


def test_all_files_are_verified_against_published_md5(tmp_path, fake_net):
    logs = []
    final = pipeline.run(md5_settings(tmp_path), logs.append, lambda *a: None)
    assert any("md5: 3 file verificati" in m and "senza md5" not in m for m in logs)
    assert sorted(set(fake_net.list_calls)) == ["b", "x"]
    assert len(fake_net.list_calls) == 2                    # un accesso per item, non uno per file
    verify_share(final, {"roms/mame/pacman.zip": b"PACZIP", "bios/dc_boot.bin": b"DCBIOS"})


def test_damaged_download_is_discarded_and_fetched_again(tmp_path, fake_net):
    fake_net.corrupt_once.add("pacman.zip")
    logs = []
    final = pipeline.run(md5_settings(tmp_path), logs.append, lambda *a: None)
    assert any("md5 diverso per pacman.zip" in m and "riscarico" in m for m in logs)
    assert fake_net.served["pacman.zip"] == 2
    verify_share(final, {"roms/mame/pacman.zip": b"PACZIP"})        # nell'immagine c'e' quello buono


def test_corrupt_file_already_in_cache_is_caught(tmp_path, fake_net):
    cached = tmp_path / "root" / "cache" / "roms" / "mame" / "pacman.zip"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"X" * len(b"PACZIP"))                      # stessa dimensione: il download lo riusa
    logs = []
    final = pipeline.run(md5_settings(tmp_path), logs.append, lambda *a: None)
    assert any("md5 diverso per pacman.zip" in m for m in logs)
    verify_share(final, {"roms/mame/pacman.zip": b"PACZIP"})


def test_persistent_mismatch_stops_the_pipeline_and_leaves_no_bad_file(tmp_path, fake_net):
    fake_net.corrupt_always.add("pacman.zip")
    with pytest.raises(pipeline.PipelineError, match=r"md5 errato per pacman\.zip.*Atteso"):
        pipeline.run(md5_settings(tmp_path), lambda m: None, lambda *a: None)
    assert fake_net.served["pacman.zip"] == 2                       # un solo nuovo tentativo
    assert not (tmp_path / "root" / "cache" / "roms" / "mame" / "pacman.zip").exists()
    assert not list((tmp_path / "root" / "out").glob("*.img"))      # niente immagine con ROM corrotte


def test_verification_can_be_turned_off(tmp_path, fake_net):
    fake_net.corrupt_always.add("pacman.zip")
    logs = []
    pipeline.run(md5_settings(tmp_path, verify_md5=False), logs.append, lambda *a: None)
    assert not any("md5:" in m or "md5 diverso" in m for m in logs)
    assert fake_net.list_calls == []                                # nemmeno si chiedono i metadati


def test_files_without_published_md5_are_reported_not_hidden(tmp_path, fake_net):
    fake_net.meta["x"] = [f for f in fake_net.meta["x"] if f.name != "pacman.zip"]   # md5 mancante
    logs = []
    pipeline.run(md5_settings(tmp_path), logs.append, lambda *a: None)
    assert any("md5: 2 file verificati, 1 senza md5 pubblicato" in m for m in logs)


def test_metadata_failure_degrades_to_unverified_with_a_clear_message(tmp_path, fake_net, monkeypatch):
    def boom(item, cache_dir=None, session=None):
        raise LookupError("item non raggiungibile")
    monkeypatch.setattr(archive, "list_files", boom)
    logs = []
    pipeline.run(md5_settings(tmp_path), logs.append, lambda *a: None)
    assert any("md5 non disponibili per l'item x" in m and "non raggiungibile" in m for m in logs)
    assert any("3 senza md5 pubblicato" in m for m in logs)


def test_md5_known_from_the_item_listing_is_used_without_asking_again(tmp_path, fake_net):
    """URL non riconoscibile come archive.org (mirror): vale l'md5 gia' noto in System.md5s."""
    url = "https://mirror.example/files/pacman.zip"
    good = md5_file(fake_net.files["pacman.zip"])
    ok = System("mame", "MAME", (url,), md5s={url: good})
    bad = System("mame", "MAME", (url,), md5s={url: "0" * 32})
    logs = []
    pipeline.run(pipeline.Settings(target_key="rg-sp", disk_gb=4, systems=[ok],
                                   root=tmp_path / "r1"), logs.append, lambda *a: None)
    assert any("md5: 1 file verificati" in m for m in logs) and fake_net.list_calls == []
    with pytest.raises(pipeline.PipelineError, match="md5 errato"):
        pipeline.run(pipeline.Settings(target_key="rg-sp", disk_gb=4, systems=[bad],
                                       root=tmp_path / "r2"), lambda m: None, lambda *a: None)


# ---------------------------------------------------------------- spazio libero sul Mac
import collections

_Usage = collections.namedtuple("Usage", "total used free")


def free_space(monkeypatch, free_bytes):
    monkeypatch.setattr(pipeline.shutil, "disk_usage", lambda p: _Usage(10**12, 0, free_bytes))


def space_settings(tmp_path, sizes=None, **kw):
    sysm = [System("mame", "MAME", ("https://archive.org/download/x/pacman.zip",), False)]
    return pipeline.Settings(target_key="rg-sp", disk_gb=4, systems=sysm, root=tmp_path / "root",
                             sizes=sizes or {}, **kw)


def test_no_room_for_the_base_image_stops_before_downloading_anything(tmp_path, fake_net, monkeypatch):
    fake_net.remote_sizes[fake_net.rel.url] = 2_350_000_000          # come l'immagine vera dell'RG SP
    free_space(monkeypatch, 3_000_000_000)                           # 3 GB: non basta per scompattarla
    with pytest.raises(pipeline.PipelineError, match=r"spazio insufficiente.*servono circa 6\.\d GB.*3\.0"):
        pipeline.run(space_settings(tmp_path), lambda m: None, lambda *a: None)
    assert fake_net.served == {}                                     # non e' stato scaricato niente


def test_no_room_for_the_roms_stops_before_downloading_them(tmp_path, fake_net, monkeypatch):
    free_space(monkeypatch, 20_000_000_000)
    url = "https://archive.org/download/x/pacman.zip"
    with pytest.raises(pipeline.PipelineError, match="spazio insufficiente") as e:
        pipeline.run(space_settings(tmp_path, sizes={url: 30_000_000_000}),
                     lambda m: None, lambda *a: None)
    assert set(fake_net.served) == {IMG_NAME}                        # l'immagine base si', le ROM no
    assert "cache/ occupa" in str(e.value) and "out/" in str(e.value)  # dice dove liberare spazio
    assert not list((tmp_path / "root" / "out").glob("*.img"))


def test_enough_room_logs_the_estimate_and_cached_files_are_not_counted(tmp_path, fake_net, monkeypatch):
    free_space(monkeypatch, 500_000_000_000)
    url = "https://archive.org/download/x/pacman.zip"
    sizes = {url: 10_000_000_000}
    logs = []
    pipeline.run(space_settings(tmp_path, sizes=sizes), logs.append, lambda *a: None)
    assert any("spazio sul Mac: servono circa 21." in m for m in logs)      # 10 da scaricare + 10,2 + margine
    # stessa richiesta con il file gia' in cache (sparso, della dimensione dichiarata): non si riconta
    cached = tmp_path / "root2" / "cache" / "roms" / "mame" / "pacman.zip"
    cached.parent.mkdir(parents=True)
    with open(cached, "wb") as f:
        f.truncate(10_000_000_000)
    logs2 = []
    s2 = space_settings(tmp_path, sizes=sizes)
    s2.root = tmp_path / "root2"
    pipeline.run(s2, logs2.append, lambda *a: None)
    assert any("spazio sul Mac: servono circa 11." in m for m in logs2)


def test_partial_download_is_credited_by_allocated_bytes(tmp_path, fake_net, monkeypatch):
    free_space(monkeypatch, 500_000_000_000)
    part = tmp_path / "root" / "cache" / "roms" / "mame" / "pacman.zip.part"
    part.parent.mkdir(parents=True)
    with open(part, "wb") as f:
        f.truncate(10_000_000_000)                                   # preallocato ma vuoto (sparso)
    logs = []
    url = "https://archive.org/download/x/pacman.zip"
    pipeline.run(space_settings(tmp_path, sizes={url: 10_000_000_000}), logs.append, lambda *a: None)
    assert any("spazio sul Mac: servono circa 21." in m for m in logs)       # sparso = non conta come scaricato


def test_unknown_sizes_are_reported(tmp_path, fake_net, monkeypatch):
    free_space(monkeypatch, 500_000_000_000)
    logs = []
    pipeline.run(space_settings(tmp_path), logs.append, lambda *a: None)
    assert any("1 file di dimensione ignota non sono contati" in m for m in logs)
