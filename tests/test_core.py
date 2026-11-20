import gzip
import io
import struct
import zipfile
from pathlib import Path

import pytest

from cm import pipeline
from cm.distro import parse_index, target_by_key
from cm.download import filename_from_url, gunzip_sparse
from cm.roms import RomsConfigError, parse_roms, safe_extract, stage_system, System


# ---------------------------------------------------------------- roms.yaml
def test_parse_ok_and_warnings():
    cfg = parse_roms({"systems": {
        "nes": {"name": "NES", "urls": ["https://archive.org/download/a/b.zip"]},
        "stranger": {"urls": ["https://example.com/x.zip"]},
        "snes": {"urls": ["https://archive.org/download/a/b.zip"]},  # duplicato
    }})
    assert cfg.systems["nes"].urls == ("https://archive.org/download/a/b.zip",)
    assert cfg.systems["snes"].urls == ()
    joined = " ".join(cfg.warnings)
    assert "stranger" in joined and "example.com" in joined and "duplicato" in joined


@pytest.mark.parametrize("bad", [
    None, {}, {"systems": []},
    {"systems": {"NES": {}}},
    {"systems": {"nes": {"urls": "http://x"}}},
    {"systems": {"nes": {"urls": ["ftp://archive.org/x"]}}},
])
def test_parse_errors(bad):
    with pytest.raises(RomsConfigError):
        parse_roms(bad)


def test_filename_from_url():
    assert filename_from_url("https://archive.org/download/i/Super%20Game%20(EU).zip") \
        == "Super Game (EU).zip"


def test_safe_extract_blocks_zip_slip(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.txt", "x")
    with zipfile.ZipFile(buf) as z, pytest.raises(RomsConfigError):
        safe_extract(z, tmp_path / "out")
    assert not (tmp_path / "evil.txt").exists()


def test_stage_extract_and_plain(tmp_path):
    cache = tmp_path / "cache"
    (cache / "nes").mkdir(parents=True)
    with zipfile.ZipFile(cache / "nes" / "set.zip", "w") as z:
        z.writestr("a.nes", "1")
    (cache / "mame").mkdir()
    (cache / "mame" / "pac.zip").write_bytes(b"zip")
    stage = tmp_path / "stage"
    stage_system(System("nes", "NES", ("https://archive.org/d/i/set.zip",), True), cache, stage)
    stage_system(System("mame", "M", ("https://archive.org/d/i/pac.zip",), False), cache, stage)
    assert (stage / "roms/nes/a.nes").read_text() == "1"
    assert (stage / "roms/mame/pac.zip").read_bytes() == b"zip"


# ---------------------------------------------------------------- distro
INDEX = '''<a href="boot.tar.xz">x</a>
<a href="batocera-h700-anbernic-rg-sp-44-20261116.img.gz">a</a>
<a href="batocera-h700-anbernic-rg-sp-43.1-20260604.img.gz">b</a>
<a href="batocera-h700-anbernic-rg-sp-44-20261116.img.gz.md5">c</a>'''


def test_parse_index_picks_images_only():
    found = parse_index(INDEX)
    assert [f[0] for f in found] == [
        "batocera-h700-anbernic-rg-sp-44-20261116.img.gz",
        "batocera-h700-anbernic-rg-sp-43.1-20260604.img.gz"]
    assert found[0][1:] == ("44", "20261116")


def test_targets():
    assert target_by_key("rg-sp").board == "anbernic-rg-sp"
    with pytest.raises(KeyError):
        target_by_key("nope")


# ---------------------------------------------------------------- pipeline
def _mbr(start2: int) -> bytes:
    mbr = bytearray(512)
    mbr[446 + 4] = 0x0C
    mbr[462 + 4] = 0x83
    mbr[462 + 8:462 + 16] = struct.pack("<II", start2, 1024)
    mbr[510:512] = b"\x55\xaa"
    return bytes(mbr)


def test_partition2_start(tmp_path):
    img = tmp_path / "i.img"
    img.write_bytes(_mbr(16779264))
    assert pipeline.partition2_start(img) == 16779264 * 512
    img.write_bytes(b"\0" * 512)
    with pytest.raises(pipeline.PipelineError):
        pipeline.partition2_start(img)


def test_share_size_for():
    MiB = 1024 * 1024
    assert pipeline.share_size_for(0) == 512 * MiB
    s = pipeline.share_size_for(10 * 1024 * MiB)
    assert s % MiB == 0 and s > 10 * 1024 * MiB


def test_gunzip_sparse_roundtrip(tmp_path):
    data = b"\0" * (3 * 1024 * 1024) + b"hello" + b"\0" * (2 * 1024 * 1024)
    src = tmp_path / "a.gz"
    src.write_bytes(gzip.compress(data))
    dst = tmp_path / "a.img"
    assert gunzip_sparse(src, dst) == len(data)
    assert dst.read_bytes() == data
