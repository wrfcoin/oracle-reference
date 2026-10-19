"""Manual/nightly live test: hits real upstream data + aviationweather.gov.

Excluded from default runs (`-m 'not live'` in pyproject). Run with:

    pytest -m live -o addopts="" tests/test_live.py

Needs the full dependency stack (CPU torch -> earth2studio) and network.
Proves the whole interpolation/units/elevation chain: /v1/reference t2m at
Duluth must agree with the same-hour KDLH METAR within ~2 °C for at least
one of hrrr/gfs.
"""

import json
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.live

# KDLH (Duluth International Airport), the station the METAR reports for.
KDLH = {"lat": 46.8369, "lon": -92.1833, "elevation_m": 435.0}
METAR_URL = "https://aviationweather.gov/api/data/metar?ids=KDLH&format=json&hours=6"
AGREEMENT_C = 2.0
# HRRR analyses publish ~1 h behind real time; test an observation old enough
# that both HRRR and GFS coverage for its hour is guaranteed.
MIN_OB_AGE = timedelta(hours=2, minutes=30)


def fetch_kdlh_metars() -> list[dict]:
    with urllib.request.urlopen(METAR_URL, timeout=30) as resp:
        return json.loads(resp.read())


def test_reference_t2m_matches_kdlh_metar():
    pytest.importorskip("earth2studio")
    metars = fetch_kdlh_metars()
    assert metars, "no KDLH METARs returned"
    # Most recent report that is old enough for analyses to exist.
    cutoff = datetime.now(tz=UTC) - MIN_OB_AGE
    metar = next(
        m
        for m in metars
        if m.get("temp") is not None
        and datetime.fromtimestamp(m["obsTime"], tz=UTC) <= cutoff
    )
    obs_time = datetime.fromtimestamp(metar["obsTime"], tz=UTC)
    hour = obs_time.replace(minute=0, second=0, microsecond=0)
    if obs_time.minute > 30:
        hour += timedelta(hours=1)

    from app.main import app

    with TestClient(app) as client:  # context manager runs lifespan (real adapters)
        body = {
            "points": [{**KDLH, "time": hour.isoformat()}],
            "variables": ["temperature_c"],
            "sources": ["hrrr", "gfs"],
        }
        resp = client.post("/v1/reference", json=body)
        assert resp.status_code == 200
        sources = resp.json()["results"][0]["sources"]

    deltas = {}
    for s in sources:
        if s["values"].get("temperature_c") is not None:
            deltas[s["source"]] = abs(s["values"]["temperature_c"] - float(metar["temp"]))
    assert deltas, f"no source returned t2m: {sources}"
    best = min(deltas.values())
    assert best <= AGREEMENT_C, (
        f"reference t2m disagrees with KDLH METAR by {best:.1f} degC "
        f"(metar {metar['temp']} at {obs_time.isoformat()}; deltas {deltas})"
    )
