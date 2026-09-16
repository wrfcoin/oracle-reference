import copy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.sources import registry
from app.sources.fixture_source import fixture_sources

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def fixture_registry():
    """Attach recorded FixtureSources to the singleton registry, restoring the
    previous state afterwards (test_health asserts the unattached default)."""
    saved_sources = registry._sources
    saved_statuses = copy.deepcopy(registry._statuses)
    registry.attach(fixture_sources(FIXTURES))
    yield registry
    registry._sources = saved_sources
    registry._statuses = saved_statuses


@pytest.fixture()
def client(fixture_registry):
    """TestClient WITHOUT lifespan (which would attach real adapters) —
    fixture sources are already attached by fixture_registry."""
    return TestClient(app)


@pytest.fixture()
def fixture_box_provider(monkeypatch):
    """Serve the recorded ERA5 box (Duluth ±6° @ 0.5°, 7 days 3-hourly) in
    place of the live provider. Raises for boxes it cannot cover, mirroring an
    upstream fetch failure (so a far-away claim degrades the correlation
    check, exactly like the live service when data is unavailable)."""
    import numpy as np

    from app import units
    from app.services import plausibility as pl

    data = np.load(FIXTURES / "era5_box_duluth.npz")
    from datetime import datetime

    box_times = [datetime.fromisoformat(str(t)) for t in data["times"]]
    box = pl.BoxSeries(
        times=box_times,
        lats=np.asarray(data["lats"], dtype=float),
        lons=np.asarray(data["lons"], dtype=float),
        t2m_c=np.asarray(data["t2m_k"], dtype=float) - units.K_OFFSET,
        mslp_hpa=np.asarray(data["msl_pa"], dtype=float) / 100.0,
    )

    def provider(source, times, lat0, lon0, radius_deg, step_deg):
        if abs(lat0 - 46.8369) > 2.0 or abs(lon0 - (-92.1833)) > 2.0:
            raise pl.PlausibilityDataError("recorded box does not cover the claimed point")
        missing = [t for t in times if t not in box.times]
        if missing:
            raise pl.PlausibilityDataError(f"recorded box lacks {len(missing)} timesteps")
        idx = [box.times.index(t) for t in times]
        return pl.BoxSeries(
            times=list(times),
            lats=box.lats,
            lons=box.lons,
            t2m_c=box.t2m_c[idx],
            mslp_hpa=box.mslp_hpa[idx],
        )

    monkeypatch.setattr(pl, "box_provider", provider)
    return box
