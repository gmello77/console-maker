import collections
from pathlib import Path

import pytest

from cm import pipeline
from cm.pipeline import ALLOC_FACTOR, SpaceNeed, allocated_bytes, check_space, space_needed

GB = 1_000_000_000
Usage = collections.namedtuple("Usage", "total used free")


def test_space_needed_math():
    n = space_needed(base_gz_size=2 * GB, base_gz_missing=2 * GB, roms_missing=3 * GB, roms_total=10 * GB)
    assert n.downloads == 5 * GB
    assert n.image == int(2 * GB * ALLOC_FACTOR) + int(10 * GB * 1.02)
    assert n.extract == 0 and n.stage_copy == 0 and n.margin == pipeline.SPACE_MARGIN
    assert n.total == n.downloads + n.image + n.margin


def test_extracted_sets_and_missing_hardlinks_cost_more():
    base = dict(base_gz_size=GB, base_gz_missing=0, roms_missing=0, roms_total=10 * GB)
    plain = space_needed(**base)
    ext = space_needed(**base, extract_zip_bytes=4 * GB)
    assert ext.extract == int(4 * GB * (2 * pipeline.EXTRACT_FACTOR - 1))   # copia in work + in piu' nell'immagine
    assert ext.total > plain.total
    copy = space_needed(**base, hardlinks=False)
    assert copy.stage_copy == 10 * GB and copy.total == plain.total + 10 * GB
    both = space_needed(**base, extract_zip_bytes=4 * GB, hardlinks=False)
    assert both.stage_copy == 6 * GB                   # le copie non riguardano gli zip scompattati


def test_cached_base_and_roms_are_not_downloaded_again():
    n = space_needed(base_gz_size=2 * GB, base_gz_missing=0, roms_missing=0, roms_total=5 * GB)
    assert n.downloads == 0


def test_allocated_bytes_sees_sparse_files_and_directories(tmp_path):
    sparse = tmp_path / "s.bin"
    with open(sparse, "wb") as f:
        f.truncate(500_000_000)
    assert sparse.stat().st_size == 500_000_000
    assert allocated_bytes(sparse) < 50_000_000          # il file sparso occupa molto meno della sua misura
    real = tmp_path / "d" / "r.bin"
    real.parent.mkdir()
    real.write_bytes(b"x" * 1_000_000)
    assert 1_000_000 <= allocated_bytes(real.parent) < 1_100_000
    assert allocated_bytes(tmp_path / "manca") == 0


def test_check_space_passes_returns_free_and_fails_with_a_useful_message(tmp_path, monkeypatch):
    need = SpaceNeed(downloads=2 * GB, image=5 * GB, extract=GB, stage_copy=GB, margin=GB)
    monkeypatch.setattr(pipeline.shutil, "disk_usage", lambda p: Usage(10**12, 0, 50 * GB))
    assert check_space(need, tmp_path) == 50 * GB
    monkeypatch.setattr(pipeline.shutil, "disk_usage", lambda p: Usage(10**12, 0, need.total))
    assert check_space(need, tmp_path) == need.total      # esattamente il necessario: va bene
    monkeypatch.setattr(pipeline.shutil, "disk_usage", lambda p: Usage(10**12, 0, need.total - 1))
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "x.bin").write_bytes(b"x" * 2_000_000)
    with pytest.raises(pipeline.PipelineError) as e:
        check_space(need, tmp_path)
    msg = str(e.value)
    assert "spazio insufficiente" in msg and "servono circa 10.0 GB" in msg
    for part in ("download 2.0 GB", "immagine 5.0 GB", "ROM scompattate 1.0 GB",
                 "copie temporanee 1.0 GB", "margine 1.0 GB"):
        assert part in msg
    assert "cache/ occupa 0.0 GB" in msg and "out/" in msg and "si riscarica" in msg


def test_measured_alloc_factor_matches_the_real_image():
    # immagine vera RG SP: 2.350.517.278 byte di .gz -> 4.966.400 BLOCCHI DA 512 BYTE occupati
    # (l'output di `ls -la` su macOS conta blocchi da 512, non kilobyte)
    measured = 4_966_400 * 512 / 2_350_517_278
    assert 1.07 < measured < 1.09
    assert measured < ALLOC_FACTOR < measured * 1.2        # prudente, ma non esagerato
