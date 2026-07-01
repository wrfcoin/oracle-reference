from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz_ok():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "cached_cycles" in body
    assert body["uptime_s"] >= 0


def test_sources_status_lists_enabled_sources():
    resp = client.get("/v1/sources/status")
    assert resp.status_code == 200
    body = resp.json()
    names = {s["name"] for s in body["sources"]}
    # defaults from ENABLED_SOURCES
    assert {"hrrr", "hrrr_fx", "gfs", "gfs_fx", "mrms", "era5"} <= names
    # no adapters yet: everything reports unavailable, never a fake cycle
    assert all(s["available"] is False for s in body["sources"])
    assert all(s["latest_cycle"] is None for s in body["sources"])
