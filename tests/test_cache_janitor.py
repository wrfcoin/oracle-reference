"""CacheJanitor against tmp_path trees with fabricated mtimes/atimes."""

import os
import time

from app.cache import CacheJanitor

NOW = time.time()
HOUR = 3600.0
DAY = 24 * HOUR


def make_file(root, rel, size, age_s, atime_age_s=None):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    ts = NOW - age_s
    os.utime(path, (NOW - (atime_age_s if atime_age_s is not None else age_s), ts))
    return path


def test_age_pass_per_source_retention(tmp_path):
    fresh_hrrr = make_file(tmp_path, "hrrr/a.grib2", 10, 1 * DAY)
    stale_hrrr = make_file(tmp_path, "hrrr/b.grib2", 10, 8 * DAY)
    fresh_gfs = make_file(tmp_path, "gfs/a.grib2", 10, 10 * DAY)  # inside 14 d
    stale_mrms = make_file(tmp_path, "mrms/a.grib2", 10, 3 * DAY)  # past 48 h
    stats = CacheJanitor(cache_dir=str(tmp_path), max_gb=1.0).run_once(now=NOW)
    assert fresh_hrrr.exists()
    assert not stale_hrrr.exists()
    assert fresh_gfs.exists()
    assert not stale_mrms.exists()
    assert stats.files_deleted == 2


def test_pinned_era5_survives_age_pass(tmp_path):
    old_arco = make_file(tmp_path, "arco/chunk.zarr", 10, 400 * DAY)
    CacheJanitor(cache_dir=str(tmp_path), max_gb=1.0).run_once(now=NOW)
    assert old_arco.exists()


def test_unknown_subdir_gets_default_retention(tmp_path):
    junk = make_file(tmp_path, "leftover/tmp.bin", 10, 30 * DAY)
    CacheJanitor(cache_dir=str(tmp_path), max_gb=1.0).run_once(now=NOW)
    assert not junk.exists()


def test_lru_pass_respects_budget_and_pins(tmp_path):
    # Budget 3 KB; 4x 1 KB fresh files -> one eviction, oldest atime first.
    pinned = make_file(tmp_path, "arco/pinned.zarr", 1000, 1 * DAY, atime_age_s=90 * DAY)
    oldest = make_file(tmp_path, "hrrr/old.grib2", 1000, 1 * DAY, atime_age_s=5 * DAY)
    newer = make_file(tmp_path, "hrrr/new.grib2", 1000, 1 * DAY, atime_age_s=1 * HOUR)
    newest = make_file(tmp_path, "gfs/new.grib2", 1000, 1 * DAY, atime_age_s=0)
    stats = CacheJanitor(cache_dir=str(tmp_path), max_gb=3e-6).run_once(now=NOW)
    assert pinned.exists()  # pinned never LRU-evicted despite oldest atime
    assert not oldest.exists()
    assert newer.exists() and newest.exists()
    assert stats.files_deleted == 1
    assert stats.bytes_after <= 3000


def test_pinned_over_budget_logs_but_never_deletes(tmp_path, caplog):
    import logging

    make_file(tmp_path, "arco/big.zarr", 5000, 1 * DAY)
    with caplog.at_level(logging.WARNING, logger="oracle-reference.cache"):
        stats = CacheJanitor(cache_dir=str(tmp_path), max_gb=1e-6).run_once(now=NOW)
    assert stats.files_deleted == 0
    assert any("pinned cache alone exceeds budget" in r.message for r in caplog.records)


def test_missing_cache_dir_is_noop(tmp_path):
    stats = CacheJanitor(cache_dir=str(tmp_path / "nope"), max_gb=1.0).run_once(now=NOW)
    assert stats.files_deleted == 0
    assert stats.bytes_before == 0
