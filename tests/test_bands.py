import json
from pathlib import Path

import pytest

from app import bands

CORE4_FIXTURE = Path(__file__).parent / "fixtures" / "reference_bands_v3.json"


def test_embedded_bands_match_core4_fixture():
    """Drift alarm: the embedded table must equal the checked-in byte copy of
    core4 shared-protocol/fixtures/reference_bands.json (commit 9ef92a235).
    If core4 recalibrates (version bump), this fails loudly."""
    upstream = json.loads(CORE4_FIXTURE.read_text())
    assert bands.as_fixture_dict() == upstream


def test_bands_version_is_3():
    assert bands.BANDS_VERSION == 3


def test_source_variable_coverage():
    assert set(bands.BANDS) == {"hrrr", "hrrr_fx", "gfs", "gfs_fx", "mrms", "era5", "nws_station"}
    assert bands.variables_for("mrms") == frozenset({"rainfall_mm"})
    assert "rainfall_mm" not in bands.variables_for("era5")
    for source in ("hrrr", "hrrr_fx", "gfs", "gfs_fx"):
        assert bands.variables_for(source) == set(bands.CANONICAL_VARIABLES)


def test_band_lookup_and_missing_pairs():
    assert bands.band("hrrr", "temperature_c") == {"good_max": 2.0, "marginal_max": 5.0}
    assert bands.band("era5", "rainfall_mm") is None
    assert bands.band("mrms", "temperature_c") is None
    assert bands.band("nonexistent", "temperature_c") is None


def test_band_ratio():
    # |1.2| / 6.0 (hrrr pressure marginal_max) = 0.2
    assert bands.band_ratio("hrrr", "pressure_hpa", 1.2) == pytest.approx(0.2)
    # Sign-insensitive.
    assert bands.band_ratio("hrrr", "pressure_hpa", -1.2) == pytest.approx(0.2)
    # Ratio 1.0 exactly at the marginal edge.
    assert bands.band_ratio("gfs", "temperature_c", 7.0) == pytest.approx(1.0)
    # No band -> None, mirroring "skip unknown pairs".
    assert bands.band_ratio("era5", "rainfall_mm", 3.0) is None


def test_forecast_bands_wider_than_analysis():
    for var in bands.CANONICAL_VARIABLES:
        assert bands.BANDS["hrrr_fx"][var]["marginal_max"] >= bands.BANDS["hrrr"][var]["marginal_max"]
        assert bands.BANDS["gfs_fx"][var]["marginal_max"] >= bands.BANDS["gfs"][var]["marginal_max"]
    # Coarser grid -> wider bands.
    assert bands.BANDS["gfs"]["temperature_c"]["good_max"] >= bands.BANDS["hrrr"]["temperature_c"]["good_max"]
