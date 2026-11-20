import zipfile

import pytest

from cm.progress import Speedometer, describe, human_bytes, human_eta
from cm.roms import (RomsConfigError, System, applicable_bios, parse_roms, stage_system)


# ---------------------------------------------------------------- BIOS: configurazione
def test_parse_bios_section():
    cfg = parse_roms({"systems": {"psx": {"urls": []}}, "bios": {
        "psx_bios": {"name": "PS1", "dir": "psx", "for": ["psx"],
                     "urls": ["https://archive.org/download/b/scph1001.bin"]},
        "pack": {"item": "bios-pack", "extract": True}}})
    b = cfg.bios["psx_bios"]
    assert (b.dir, b.only_for, b.name) == ("psx", ("psx",), "PS1")
    assert cfg.bios["pack"].item == "bios-pack" and cfg.bios["pack"].dir == ""
    assert cfg.warnings == []
    assert parse_roms({"systems": {}}).bios == {}


@pytest.mark.parametrize("bad", [
    {"dir": "../fuori"}, {"dir": "/assoluta"}, {"dir": "a/../../b"}, {"dir": "a\\b"}, {"dir": 5},
    {"for": "psx"}, {"item": "../x"}, {"urls": ["ftp://archive.org/x"]},
])
def test_parse_bios_rejects_unsafe_or_malformed(bad):
    with pytest.raises(RomsConfigError):
        parse_roms({"systems": {}, "bios": {"g": bad}})
    with pytest.raises(RomsConfigError):
        parse_roms({"systems": {}, "bios": ["lista"]})


def test_bios_warns_about_unknown_systems_and_duplicate_urls():
    u = "https://archive.org/download/b/x.bin"
    cfg = parse_roms({"systems": {"nes": {"urls": [u]}}, "bios": {
        "g": {"for": ["nes", "fantasma"], "urls": [u]}}})
    text = " ".join(cfg.warnings)
    assert "fantasma" in text and "duplicato" in text


def test_applicable_bios():
    a = System("a", "A", only_for=("psx",))
    b = System("b", "B")                       # senza 'for': sempre
    c = System("c", "C", only_for=("saturn", "psx"))
    assert applicable_bios([a, b, c], {"nes"}) == [b]
    assert applicable_bios([a, b, c], {"psx"}) == [a, b, c]
    assert applicable_bios([], {"psx"}) == []


def test_stage_bios_dirs_and_extract(tmp_path):
    cache, stage = tmp_path / "cache", tmp_path / "stage"
    (cache / "root").mkdir(parents=True)
    (cache / "root" / "neogeo.zip").write_bytes(b"NG")
    (cache / "dc").mkdir()
    with zipfile.ZipFile(cache / "dc" / "pack.zip", "w") as z:
        z.writestr("dc_boot.bin", "D")
    stage_system(System("root", "R", ("https://x/neogeo.zip",)), cache, stage, area="bios")
    stage_system(System("dc", "D", ("https://x/pack.zip",), extract=True, dir="dc"),
                 cache, stage, area="bios")
    assert (stage / "bios" / "neogeo.zip").read_bytes() == b"NG"        # dir vuota: radice di bios
    assert (stage / "bios" / "dc" / "dc_boot.bin").read_text() == "D"
    assert not (stage / "roms").exists()


# ---------------------------------------------------------------- avanzamento
def test_human_formats():
    assert human_bytes(0) == "0 B" and human_bytes(999) == "999 B"
    assert human_bytes(1_500) == "2 KB" and human_bytes(2_500_000) == "2.5 MB"
    assert human_bytes(3_200_000_000) == "3.2 GB" and human_bytes(5e12) == "5000.0 GB"
    assert human_eta(None) == "--" and human_eta(-1) == "--" and human_eta(float("inf")) == "--"
    assert human_eta(7) == "7s" and human_eta(125) == "2m05s" and human_eta(3700) == "1h01m"


def test_speedometer_speed_eta_and_resets():
    sp = Speedometer(min_interval=0.25)
    assert sp.update("a", 0, now=0.0) == 0.0                  # primo campione: ancora ignota
    assert sp.update("a", 1_000_000, now=1.0) == pytest.approx(1_000_000)
    assert sp.eta(1_000_000, 11_000_000) == pytest.approx(10.0)
    assert sp.update("a", 1_100_000, now=1.1) == pytest.approx(1_000_000)   # troppo presto: invariata
    assert sp.update("a", 3_000_000, now=2.0) > 1_000_000     # accelera: la media sale, ma non di colpo
    assert sp.update("b", 10, now=3.0) == 0.0                 # nuovo file: si riparte
    assert sp.eta(10, 100) is None                            # senza velocita' niente ETA
    sp.update("b", 5_000, now=4.0)
    assert sp.update("b", 100, now=5.0) == 0.0                # byte all'indietro (nuovo tentativo)
    assert sp.eta(0, 0) is None


def test_describe_line():
    assert describe("ROM nes: a.zip", 5_000_000, 10_000_000, 0, None) == \
        "ROM nes: a.zip  5.0 MB / 10.0 MB"
    line = describe("ROM nes: a.zip", 5_000_000, 10_000_000, 2_500_000, 2.0)
    assert "2.5 MB/s" in line and "ancora 2s" in line
    assert describe("x", 3_000, 0, 0, None) == "x  3 KB"      # totale ignoto
