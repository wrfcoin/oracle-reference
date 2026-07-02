"""Real GRIB2 decode of the checked-in MRMS sample. Needs cfgrib/eccodes —
auto-skips under slim CI deps."""

import gzip
import shutil
from pathlib import Path

import numpy as np
import pytest

from app.sources.mrms import decode_grib_tile

pytestmark = pytest.mark.grib

FIXTURE_GZ = Path(__file__).parent / "fixtures" / "mrms_sample.grib2.gz"
DULUTH = (46.8369, -92.1833)


@pytest.fixture()
def grib_path(tmp_path):
    pytest.importorskip("cfgrib")
    if not FIXTURE_GZ.exists():
        pytest.skip("mrms_sample.grib2.gz not recorded")
    out = tmp_path / "mrms_sample.grib2"
    with gzip.open(FIXTURE_GZ, "rb") as gz, open(out, "wb") as fh:
        shutil.copyfileobj(gz, fh)
    return out


def test_decode_tile_around_duluth(grib_path):
    lats, lons, tile = decode_grib_tile(grib_path, *DULUTH, half_deg=0.06)
    assert np.all(np.diff(lats) > 0)
    assert np.all(np.diff(lons) > 0)
    assert tile.shape == (len(lats), len(lons))
    assert tile.shape[0] >= 2 and tile.shape[1] >= 2
    finite = tile[np.isfinite(tile)]
    assert finite.size > 0
    assert finite.min() >= 0.0  # negative no-coverage sentinels became NaN
    assert finite.max() < 300.0  # mm/h sanity


def test_decode_point_outside_coverage_raises(grib_path):
    from app.sources.base import SourceFetchError

    with pytest.raises(SourceFetchError):
        decode_grib_tile(grib_path, 10.0, 100.0, half_deg=0.06)  # mid-Pacific
