"""POST /v1/reference against recorded fixtures."""

import numpy as np
import pytest

from app import hypso, units
from app.interp import bilinear

DULUTH = {"lat": 46.8369, "lon": -92.1833, "elevation_m": 186.0, "time": "2024-06-24T12:00:00Z"}


def post(client, **overrides):
    body = {"points": [DULUTH], **overrides}
    return client.post("/v1/reference", json=body)


def test_reference_basic_shape(client):
    resp = post(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"]["bands_version"] == 1
    (result,) = body["results"]
    assert result["point"]["lat"] == DULUTH["lat"]
    by_source = {s["source"]: s for s in result["sources"]}
    assert {"hrrr", "hrrr_fx", "gfs", "gfs_fx", "mrms", "era5"} <= set(by_source)
    hrrr = by_source["hrrr"]
    assert hrrr["error"] is None
    assert set(hrrr["values"]) == {
        "temperature_c", "pressure_hpa", "humidity_pct", "wind_speed_ms", "rainfall_mm"
    }
    assert hrrr["cycle"].startswith("2024-06-24")
    assert hrrr["grid"]["nearest_distance_km"] < 25.0
    assert hrrr["grid"]["resolution_deg"] > 0
    # Sanity: mild June morning by Lake Superior.
    assert 5.0 < hrrr["values"]["temperature_c"] < 25.0
    assert 900.0 < hrrr["values"]["pressure_hpa"] < 1050.0


def test_lead_hours_semantics(client):
    body = post(client).json()
    by_source = {s["source"]: s for s in body["results"][0]["sources"]}
    # Analysis sources: instant variables at lead 0.
    assert by_source["era5"]["leads"]["temperature_c"] == 0.0
    assert by_source["hrrr"]["leads"]["temperature_c"] == 0.0
    # HRRR rainfall comes from f01 of the prior cycle.
    assert by_source["hrrr"]["leads"]["rainfall_mm"] == 1.0
    # Forecast sources report positive leads; per-source lead_hours is the max.
    assert by_source["hrrr_fx"]["lead_hours"] >= 1.0
    assert by_source["gfs_fx"]["lead_hours"] >= 1.0
    for s in body["results"][0]["sources"]:
        if s["values"]:
            assert s["lead_hours"] == max(s["leads"].values())


def test_pressure_corrected_to_station_elevation(client):
    """The returned pressure must equal MSLP hypsometrically corrected to the
    station elevation, using the source's own t2m for the column temperature."""
    body = post(client, variables=["pressure_hpa", "temperature_c"]).json()
    hrrr = {s["source"]: s for s in body["results"][0]["sources"]}["hrrr"]
    assert hrrr["grid"]["elevation_correction_applied"]["pressure_hpa"] is True
    assert hrrr["grid"]["grid_elevation_m"] == 0.0  # MSLP datum

    from app.sources.fixture_source import FixtureSource
    from pathlib import Path
    from datetime import datetime, timezone

    src = FixtureSource("hrrr", Path(__file__).parent / "fixtures")
    t = datetime(2024, 6, 24, 12, tzinfo=timezone.utc)
    p_sample = src.fetch(t, "pressure_hpa", DULUTH["lat"], DULUTH["lon"])
    t_sample = src.fetch(t, "temperature_c", DULUTH["lat"], DULUTH["lon"])
    mslp = bilinear(p_sample.lats, p_sample.lons, p_sample.values, DULUTH["lat"], DULUTH["lon"]).value
    t_c = bilinear(t_sample.lats, t_sample.lons, t_sample.values, DULUTH["lat"], DULUTH["lon"]).value
    t_sl_k = units.c_to_k(t_c) + hypso.LAPSE * DULUTH["elevation_m"]
    expected = hypso.correct_pressure_to_station(mslp, 0.0, DULUTH["elevation_m"], t_sl_k)
    assert hrrr["values"]["pressure_hpa"] == pytest.approx(expected, abs=0.01)
    # Correction is downward-in-pressure for a station above sea level.
    assert hrrr["values"]["pressure_hpa"] < mslp


def test_pressure_uncorrected_without_elevation(client):
    point = {k: v for k, v in DULUTH.items() if k != "elevation_m"}
    body = client.post(
        "/v1/reference", json={"points": [point], "variables": ["pressure_hpa"]}
    ).json()
    hrrr = {s["source"]: s for s in body["results"][0]["sources"]}["hrrr"]
    assert hrrr["grid"]["elevation_correction_applied"]["pressure_hpa"] is False


def test_wind_interpolates_components(client):
    """Endpoint wind must equal hypot(interp(u), interp(v)), not interp(speed)."""
    body = post(client, variables=["wind_speed_ms"], sources=["gfs"]).json()
    gfs = body["results"][0]["sources"][0]

    from app.sources.fixture_source import FixtureSource
    from pathlib import Path
    from datetime import datetime, timezone

    src = FixtureSource("gfs", Path(__file__).parent / "fixtures")
    s = src.fetch(datetime(2024, 6, 24, 12, tzinfo=timezone.utc), "wind_speed_ms",
                  DULUTH["lat"], DULUTH["lon"])
    u = bilinear(s.lats, s.lons, s.components["u"], DULUTH["lat"], DULUTH["lon"]).value
    v = bilinear(s.lats, s.lons, s.components["v"], DULUTH["lat"], DULUTH["lon"]).value
    assert gfs["values"]["wind_speed_ms"] == pytest.approx(np.hypot(u, v), abs=1e-3)


def test_source_filter_and_variable_skipping(client):
    body = post(client, sources=["mrms", "era5"]).json()
    by_source = {s["source"]: s for s in body["results"][0]["sources"]}
    assert set(by_source) == {"mrms", "era5"}
    assert set(by_source["mrms"]["values"]) == {"rainfall_mm"}
    assert "rainfall_mm" not in by_source["era5"]["values"]


def test_source_serving_nothing_reports_error(client):
    body = post(client, variables=["temperature_c"], sources=["mrms"]).json()
    (mrms,) = body["results"][0]["sources"]
    assert mrms["values"] == {}
    assert "none of the requested variables" in mrms["error"]


def test_validation_errors(client):
    bad_lat = dict(DULUTH, lat=123.0)
    assert post(client, points=[bad_lat]).status_code == 422
    naive_time = dict(DULUTH, time="2024-06-24T12:00:00")
    assert post(client, points=[naive_time]).status_code == 422
    assert post(client, variables=["temperature_f"]).status_code == 422
    assert post(client, sources=["ecmwf_secret"]).status_code == 422
    assert client.post("/v1/reference", json={"points": []}).status_code == 422
    too_many = {"points": [DULUTH] * 101}
    assert client.post("/v1/reference", json=too_many).status_code == 422


def test_unattached_source_degrades_not_fails(client):
    """A source with no adapter yields an error entry, not a failed request."""
    from app.sources import registry

    registry._sources.pop("gfs")
    body = post(client, sources=["gfs", "hrrr"]).json()
    by_source = {s["source"]: s for s in body["results"][0]["sources"]}
    assert by_source["gfs"]["error"] is not None
    assert by_source["gfs"]["values"] == {}
    assert by_source["hrrr"]["error"] is None


def test_lon_0_360_accepted(client):
    wrapped = dict(DULUTH, lon=DULUTH["lon"] + 360.0)
    body = post(client, points=[wrapped], variables=["temperature_c"], sources=["hrrr"]).json()
    hrrr = body["results"][0]["sources"][0]
    assert hrrr["error"] is None
    ref = post(client, variables=["temperature_c"], sources=["hrrr"]).json()
    assert hrrr["values"]["temperature_c"] == ref["results"][0]["sources"][0]["values"]["temperature_c"]
