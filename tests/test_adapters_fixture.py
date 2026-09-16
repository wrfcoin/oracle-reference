"""GridSample invariants over every recorded fixture, via FixtureSource."""

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from app.sources.base import GridSample, SourceFetchError
from app.sources.fixture_source import FixtureSource, fixture_sources

FIXTURES = Path(__file__).parent / "fixtures"
DULUTH = (46.8369, -92.1833)
ANY_TIME = datetime(2024, 6, 24, 12, tzinfo=timezone.utc)

# Physical sanity ranges per canonical variable (canonical units).
RANGES = {
    "temperature_c": (-60.0, 55.0),
    "pressure_hpa": (850.0, 1090.0),  # MSLP datum
    "humidity_pct": (0.0, 100.0),
    "wind_speed_ms": (0.0, 75.0),
    "rainfall_mm": (0.0, 300.0),
}


def all_samples():
    out = []
    for name, src in fixture_sources(FIXTURES).items():
        for var in sorted(src.variables):
            out.append((name, var, src.fetch(ANY_TIME, var, *DULUTH)))
    return out


def test_manifest_covers_expected_sources():
    names = set(fixture_sources(FIXTURES))
    assert {"hrrr", "hrrr_fx", "gfs", "gfs_fx", "era5", "mrms"} <= names


def test_grid_sample_invariants():
    samples = all_samples()
    assert samples, "no fixtures recorded"
    for name, var, s in samples:
        assert isinstance(s, GridSample), (name, var)
        assert s.values.ndim == 2, (name, var)
        assert s.values.shape[0] >= 2 and s.values.shape[1] >= 2, (name, var)
        if s.lats.ndim == 1:
            assert np.all(np.diff(s.lats) > 0), f"{name}/{var}: lats not ascending"
            assert np.all(np.diff(s.lons) > 0), f"{name}/{var}: lons not ascending"
            assert s.values.shape == (len(s.lats), len(s.lons)), (name, var)
        else:
            assert s.lats.shape == s.lons.shape == s.values.shape, (name, var)
        assert np.all(s.lons >= -180.0) and np.all(s.lons < 180.0), (name, var)
        lo, hi = RANGES[var]
        finite = s.values[np.isfinite(s.values)]
        assert finite.size > 0, f"{name}/{var}: all-NaN tile"
        assert finite.min() >= lo and finite.max() <= hi, (
            f"{name}/{var}: values outside {lo}..{hi}: {finite.min()}..{finite.max()}"
        )
        assert s.resolution_deg > 0, (name, var)
        assert s.lead_hours >= 0, (name, var)
        assert s.cycle.tzinfo is not None and s.valid_time.tzinfo is not None, (name, var)


def test_tile_encloses_station_point():
    lat, lon = DULUTH
    for name, var, s in all_samples():
        assert s.lats.min() <= lat <= s.lats.max(), f"{name}/{var} tile misses station lat"
        assert s.lons.min() <= lon <= s.lons.max(), f"{name}/{var} tile misses station lon"


def test_pressure_tiles_are_mslp_with_zero_datum():
    for name, var, s in all_samples():
        if var != "pressure_hpa":
            continue
        assert s.grid_elevation_m is not None, name
        assert np.all(s.grid_elevation_m == 0.0), f"{name}: pressure datum must be sea level"


def test_wind_tiles_carry_components():
    for name, var, s in all_samples():
        if var != "wind_speed_ms":
            continue
        assert s.components is not None and set(s.components) == {"u", "v"}, name
        speed = np.hypot(s.components["u"], s.components["v"])
        assert np.allclose(speed, s.values, equal_nan=True), f"{name}: speed != hypot(u, v)"


def test_forecast_sources_report_positive_lead():
    for name, var, s in all_samples():
        if name in ("hrrr_fx", "gfs_fx"):
            assert s.lead_hours >= 1.0, (name, var)
        if name in ("hrrr", "era5") and var != "rainfall_mm":
            assert s.lead_hours == 0.0, (name, var)


def test_fixture_source_unknown_variable_raises():
    src = FixtureSource("mrms", FIXTURES)
    with pytest.raises(SourceFetchError):
        src.fetch(ANY_TIME, "temperature_c", *DULUTH)


def test_latest_cycle_reported():
    for name, src in fixture_sources(FIXTURES).items():
        assert src.latest_cycle() is not None, name
