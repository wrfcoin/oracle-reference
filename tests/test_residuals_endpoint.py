"""POST /v1/residuals against recorded fixtures — including a contract test
that reproduces PR #6's agent/attestation_loop.py::reference_check parsing."""

import pytest

TIME = "2024-06-24T12:00:00Z"
DULUTH = {"lat": 46.8369, "lon": -92.1833, "elevation_m": 186.0, "time": TIME}


def reference_values(client, variables=None, sources=None):
    body = {"points": [DULUTH]}
    if variables:
        body["variables"] = variables
    if sources:
        body["sources"] = sources
    resp = client.post("/v1/reference", json=body).json()
    return {s["source"]: s for s in resp["results"][0]["sources"]}


def post_residuals(client, observation, **overrides):
    body = {"observations": [{**DULUTH, "observation": observation}], **overrides}
    return client.post("/v1/residuals", json=body)


def test_residuals_are_observed_minus_reference_canonical_units(client):
    refs = reference_values(client)
    obs = {"temperature_c": refs["hrrr"]["values"]["temperature_c"] + 1.5,
           "pressure_hpa": refs["hrrr"]["values"]["pressure_hpa"] - 2.0}
    body = post_residuals(client, obs).json()
    (result,) = body["results"]
    hrrr = {s["source"]: s for s in result["per_source"]}["hrrr"]
    assert hrrr["residuals"]["temperature_c"] == pytest.approx(1.5, abs=1e-3)
    assert hrrr["residuals"]["pressure_hpa"] == pytest.approx(-2.0, abs=1e-3)
    # Sign preserved; ratios are |residual| / marginal_max from bands v3.
    assert hrrr["band_ratios"]["temperature_c"] == pytest.approx(1.5 / 5.0, abs=1e-3)
    assert hrrr["band_ratios"]["pressure_hpa"] == pytest.approx(2.0 / 6.0, abs=1e-3)
    assert hrrr["reference"]["temperature_c"] == refs["hrrr"]["values"]["temperature_c"]
    assert result["bands_version"] == 3


def test_band_pairs_without_bands_are_skipped(client):
    body = post_residuals(client, {"rainfall_mm": 0.0}).json()
    by_source = {s["source"]: s for s in body["results"][0]["per_source"]}
    assert "rainfall_mm" in by_source["mrms"]["residuals"]
    # era5 has no rainfall band: the source entry reports an error, no residual.
    if "era5" in by_source:
        assert "rainfall_mm" not in by_source["era5"]["residuals"]


def test_worst_is_max_ratio_across_sources_and_variables(client):
    refs = reference_values(client)
    obs = {"temperature_c": refs["hrrr"]["values"]["temperature_c"] + 8.0}  # hrrr ratio 1.6
    body = post_residuals(client, obs, sources=["hrrr"], mode="ratio").json()
    (result,) = body["results"]
    assert result["worst"]["source"] == "hrrr"
    assert result["worst"]["variable"] == "temperature_c"
    assert result["worst_ratio"] == pytest.approx(8.0 / 5.0, abs=1e-2)
    assert result["worst_ratio"] > result["marginal_threshold"]


def test_ratio_mode_thresholds_are_constants(client):
    body = post_residuals(client, {"temperature_c": 14.0}, mode="ratio").json()
    (result,) = body["results"]
    assert result["marginal_threshold"] == 1.0
    assert result["outlier_threshold"] == 2.0


def test_default_raw_mode_decides_exactly_like_ratio_mode(client):
    """Default (raw) thresholds are scaled per result so a raw-residual
    comparison is mathematically equivalent to the band-ratio comparison."""
    refs = reference_values(client)
    cases = [
        {"temperature_c": refs["hrrr"]["values"]["temperature_c"] + 0.5,
         "humidity_pct": refs["hrrr"]["values"]["humidity_pct"] - 13.0},  # good, mixed units
        {"temperature_c": refs["hrrr"]["values"]["temperature_c"] + 6.5},  # marginal-ish
        {"temperature_c": 40.0, "pressure_hpa": 950.0},  # fraud
    ]
    for obs in cases:
        raw = post_residuals(client, obs).json()["results"][0]
        ratio = post_residuals(client, obs, mode="ratio").json()["results"][0]
        approved_raw, conf_raw = _reference_check_pr6(raw, "residuals")
        approved_r, conf_r = _reference_check_pr6(ratio, "band_ratios")
        assert approved_raw == approved_r, obs
        assert conf_raw == pytest.approx(conf_r, abs=1e-3), obs


def test_flattened_core4_reading_shape_accepted(client):
    """core4 canonical readings carry variables at the top level."""
    reading = {**DULUTH, "temperature_c": 14.0, "pressure_hpa": 990.0}
    body = client.post("/v1/residuals", json={"observations": [reading]}).json()
    hrrr = {s["source"]: s for s in body["results"][0]["per_source"]}["hrrr"]
    assert "temperature_c" in hrrr["residuals"]
    assert "pressure_hpa" in hrrr["residuals"]


def test_observed_at_unix_accepted(client):
    import datetime as dt

    ts = int(dt.datetime(2024, 6, 24, 12, tzinfo=dt.UTC).timestamp())
    for stamp in (ts, ts * 1000):  # seconds and milliseconds
        ob = {"lat": DULUTH["lat"], "lon": DULUTH["lon"], "elevation_m": 186.0,
              "observed_at_unix": stamp, "observation": {"temperature_c": 14.0}}
        resp = client.post("/v1/residuals", json={"observations": [ob]})
        assert resp.status_code == 200, stamp


def test_validation_errors(client):
    no_time = {"lat": 46.8, "lon": -92.1, "observation": {"temperature_c": 1.0}}
    assert client.post("/v1/residuals", json={"observations": [no_time]}).status_code == 422
    bad_var = {**DULUTH, "observation": {"temperature_f": 60.0}}
    assert client.post("/v1/residuals", json={"observations": [bad_var]}).status_code == 422
    empty = {**DULUTH, "observation": {}}
    assert client.post("/v1/residuals", json={"observations": [empty]}).status_code == 422


# --------------------------------------------------------------------------
# PR #6 contract tests: reproduce agent/attestation_loop.py::reference_check
# parsing byte-for-byte. `agent/` lives on the still-open PR #6 branch, so the
# logic is mirrored here rather than imported.
# --------------------------------------------------------------------------


def _reference_check_pr6(result: dict, key: str) -> tuple[bool, float]:
    """PR #6's parsing; `key` is "residuals" (PR #6 as written — correct
    against the default raw mode) or "band_ratios" (the dimensionless
    contract, paired with mode="ratio")."""
    worst = max((abs(r) for src in result["per_source"] for r in src[key].values()), default=0.0)
    approved = worst <= result.get("marginal_threshold", float("inf"))
    confidence = max(0.0, min(1.0, 1.0 - worst / (result.get("outlier_threshold", 1.0) or 1.0)))
    return approved, round(confidence, 3)


def good_reading(client):
    refs = reference_values(client)
    return {
        "temperature_c": refs["hrrr"]["values"]["temperature_c"] + 0.5,
        "pressure_hpa": refs["hrrr"]["values"]["pressure_hpa"] + 0.5,
        "humidity_pct": refs["hrrr"]["values"]["humidity_pct"] - 3.0,
        "wind_speed_ms": max(0.0, refs["hrrr"]["values"]["wind_speed_ms"] - 0.5),
    }


def test_pr6_reference_check_ratio_mode_with_band_ratios(client):
    """mode=ratio + the documented one-line PR #6 change (band_ratios)."""
    ob = {**DULUTH, "observation": good_reading(client)}
    body = {"observations": [ob], "mode": "ratio"}
    result = client.post("/v1/residuals", json=body).json()["results"][0]
    approved, confidence = _reference_check_pr6(result, "band_ratios")
    assert approved
    assert confidence > 0.5

    fraud = {**DULUTH, "observation": {"temperature_c": 40.0, "pressure_hpa": 950.0}}
    body = {"observations": [fraud], "mode": "ratio"}
    result = client.post("/v1/residuals", json=body).json()["results"][0]
    approved, confidence = _reference_check_pr6(result, "band_ratios")
    assert not approved
    assert confidence == 0.0


def test_pr6_reference_check_verbatim_against_default_mode(client):
    """PR #6 as written today ({"observations": [reading]}, no mode; compares
    raw residuals) must decide correctly against the default response."""
    ob = {**DULUTH, "observation": good_reading(client)}
    result = client.post("/v1/residuals", json={"observations": [ob]}).json()["results"][0]
    approved, confidence = _reference_check_pr6(result, "residuals")
    assert approved
    assert confidence > 0.0

    fraud = {**DULUTH, "observation": {"temperature_c": 40.0, "pressure_hpa": 950.0}}
    result = client.post("/v1/residuals", json={"observations": [fraud]}).json()["results"][0]
    approved, _ = _reference_check_pr6(result, "residuals")
    assert not approved


def test_pr6_request_shape_is_accepted_verbatim(client):
    """PR #6 sends exactly {"observations": [reading]} — no sources/mode."""
    reading = {**DULUTH, "observation": {"temperature_c": 14.0}}
    resp = client.post("/v1/residuals", json={"observations": [reading]})
    assert resp.status_code == 200
    (result,) = resp.json()["results"]
    assert "per_source" in result
    assert "marginal_threshold" in result and "outlier_threshold" in result


def test_advisory_tier_metadata_is_additive(client):
    ob = {**DULUTH, "observation": {"temperature_c": 99.0}}
    result = client.post("/v1/residuals", json={"observations": [ob]}).json()["results"][0]
    assert result["tier"] == "outlier"
    assert result["bands_version"] == 3
    assert result["fold_rule"] == "second_worst"
    assert result["gross_guard_active"] is False
    assert "worst_ratio" in result and "marginal_threshold" in result


@pytest.mark.parametrize("height, expected", [(99, "degraded"), (100, "outlier"), (101, "outlier")])
def test_endpoint_selects_guard_at_activation_height(client, monkeypatch, height, expected):
    from app.models import PointResult, SourceValues
    from app.services import residuals
    monkeypatch.setattr(residuals, "compute_reference", lambda points, *_: [PointResult(
        point=points[0], sources=[SourceValues(source="nws_station", values={"temperature_c": 0, "humidity_pct": 0})]
    )])
    body = {"observations": [{**DULUTH, "observation": {"temperature_c": 10, "humidity_pct": 1}}],
            "sources": ["nws_station"], "tiering_context": {"height": height, "gross_guard_activation_height": 100}}
    response = client.post("/v1/residuals", json=body)
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["tier"] == expected
    assert result["gross_guard_active"] is (height >= 100)
    assert result["worst_ratio"] == 1.6667


@pytest.mark.parametrize("context", [{"height": -1}, {"height": 1.5}, {"height": True}, {"height": 1, "gross_guard_activation_height": -1}])
def test_invalid_activation_context_is_rejected(client, context):
    assert post_residuals(client, {"temperature_c": 1}, tiering_context=context).status_code == 422
