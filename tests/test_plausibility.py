"""POST /v1/location-plausibility — issue #3 acceptance demo included."""

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

DULUTH = {"lat": 46.8369, "lon": -92.1833, "elevation_m": 430.0}  # KDLH-like station
DENVER = {"lat": 39.7392, "lon": -104.9903, "elevation_m": 1609.0}


@pytest.fixture()
def duluth_series():
    return json.loads((FIXTURES / "duluth_7day_series.json").read_text())["series"]


def post(client, claimed, series, **options):
    body = {"claimed": claimed, "series": series}
    if options:
        body["options"] = options
    return client.post("/v1/location-plausibility", json=body)


def test_acceptance_demo_duluth_series_claimed_denver(client, fixture_box_provider, duluth_series):
    """Issue #3: Duluth series with claimed Denver coords -> inconsistent,
    implied elevation ~430 m vs claimed ~1600 m."""
    resp = post(client, DENVER, duluth_series)
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "inconsistent"
    elev = body["checks"]["pressure_elevation"]
    assert elev["available"] is True
    assert 150.0 <= elev["implied_elevation_m"] <= 600.0
    assert elev["delta_m"] < -500.0  # hard rule trips regardless of correlation
    assert "grid cell" in body["limitations"]


def test_true_duluth_claim_is_consistent(client, fixture_box_provider, duluth_series):
    resp = post(client, DULUTH, duluth_series)
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "consistent", body["checks"]
    elev = body["checks"]["pressure_elevation"]
    # Reference-MSLP-anchored inversion recovers the true 430 m station.
    assert elev["implied_elevation_m"] == pytest.approx(430.0, abs=60.0)
    assert abs(elev["delta_m"]) <= 250.0
    corr = body["checks"]["temperature_correlation"]
    assert corr["available"] is True
    assert corr["best_corr"] > 0.5
    assert corr["corr_at_claimed"] > 0.4 * corr["best_corr"]
    assert body["plausibility"] >= 0.7


def test_correlation_localizes_near_station(client, fixture_box_provider, duluth_series):
    body = post(client, DULUTH, duluth_series).json()
    corr = body["checks"]["temperature_correlation"]
    # Best match within a few grid cells of the true location (~0.5 deg grid).
    assert corr["best_match_distance_km"] <= 250.0


def test_short_series_rejected(client, fixture_box_provider, duluth_series):
    resp = post(client, DULUTH, duluth_series[:20])  # ~2.5 days
    assert resp.status_code == 422
    assert "7 days" in resp.json()["detail"]


def test_series_without_temperature_rejected(client, fixture_box_provider, duluth_series):
    stripped = [
        {"time": s["time"], "observation": {"pressure_hpa": s["observation"]["pressure_hpa"]}}
        for s in duluth_series
    ]
    resp = post(client, DULUTH, stripped)
    assert resp.status_code == 422
    assert "temperature_c" in resp.json()["detail"]


def test_no_pressure_drops_elevation_check(client, fixture_box_provider, duluth_series):
    no_pressure = [
        {"time": s["time"], "observation": {"temperature_c": s["observation"]["temperature_c"]}}
        for s in duluth_series
    ]
    body = post(client, DULUTH, no_pressure).json()
    assert body["checks"]["pressure_elevation"]["available"] is False
    assert body["checks"]["temperature_correlation"]["available"] is True
    assert "pressure_hpa" not in body["series"]["variables_used"]


def test_box_unavailable_still_flags_elevation_fraud(client, fixture_box_provider, duluth_series):
    """Claimed point far outside the recorded box: correlation degrades, but
    the standard-atmosphere elevation inversion still exposes the mismatch."""
    body = post(client, DENVER, duluth_series).json()
    assert body["checks"]["temperature_correlation"]["available"] is False
    assert body["verdict"] == "inconsistent"


def test_naive_timestamps_rejected(client, fixture_box_provider):
    series = [{"time": "2024-06-17T00:00:00", "observation": {"temperature_c": 15.0}}]
    assert post(client, DULUTH, series).status_code == 422
