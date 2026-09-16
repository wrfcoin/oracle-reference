import argparse
import json
import stat

import pytest

from agent import attestation_loop as loop
from agent.attestation_loop import (
    Config,
    build_observation,
    canonical_variables,
    decide_from_result,
    reference_check,
    select_pending,
    sign_attestation,
)


def _cfg(monkeypatch, **over):
    unsafe = over.pop("unsafe_approve_all", False)
    submit = over.pop("submit", False)
    args = argparse.Namespace(
        interval=60.0, unsafe_approve_all=unsafe, submit=submit, manual_pending=None
    )
    for k, v in over.items():
        monkeypatch.setenv(k, v)
    return Config.from_env(args)


def _pending(**over):
    base = {
        "chain_id": "wrf-testnet",
        "network_id": "testnet",
        "height": 100,
        "oracle_set": ["oracle-a", "oracle-b"],
        "oracle_quorum": 2,
        "effective_quorum": 2,
        "observations": [
            {
                "commit_id": "c-fresh",
                "station_id": "STN1",
                "timestamp": 1_758_000_000,
                "nonce": "7",
                "contribution_type": "weather",
                "data": {"temperature_c": 21.5, "humidity": 55.0, "pressure_hpa": 1008.2},
                "source": "mempool",
                "attesting_oracles": [],
                "has_quorum": False,
                "reward_recorded": False,
                "reward_finalized": False,
            },
            {
                "commit_id": "c-mine",
                "station_id": "STN1",
                "timestamp": 1_758_000_001,
                "nonce": "8",
                "contribution_type": "weather",
                "data": {"temperature_c": 21.0},
                "source": "block",
                "attesting_oracles": ["oracle-a"],
                "has_quorum": False,
                "reward_recorded": False,
                "reward_finalized": False,
            },
            {
                "commit_id": "c-quorum",
                "station_id": "STN2",
                "timestamp": 1_758_000_002,
                "nonce": "9",
                "contribution_type": "weather",
                "data": {"temperature_c": 20.0},
                "source": "block",
                "attesting_oracles": ["oracle-b", "oracle-c"],
                "has_quorum": True,
                "reward_recorded": True,
                "reward_finalized": False,
            },
        ],
        "ready_to_mine": [],
        "unrecoverable": ["c-lost"],
    }
    base.update(over)
    return base


# ------------------------------------------------------------------ discovery


def test_select_pending_skips_quorum_settled_and_self_attested(monkeypatch):
    cfg = _cfg(monkeypatch, ORACLE_TOKEN="t", ORACLE_PRIVKEY="pk", ORACLE_ID="oracle-a")
    items, skips = select_pending(cfg, _pending())
    assert [i["commit_id"] for i in items] == ["c-fresh"]
    assert items[0]["reading"] == {"temperature_c": 21.5, "humidity": 55.0, "pressure_hpa": 1008.2}
    assert items[0]["nonce"] == "7" and items[0]["station_id"] == "STN1"
    assert dict(skips) == {
        "c-mine": "already attested by this oracle",
        "c-quorum": "already at quorum",
        "c-lost": "node reports reveal payload unrecoverable",
    }


def test_select_pending_refuses_when_oracle_not_in_set(monkeypatch):
    cfg = _cfg(monkeypatch, ORACLE_TOKEN="t", ORACLE_PRIVKEY="pk", ORACLE_ID="oracle-z")
    with pytest.raises(SystemExit, match="not in the node's oracle_set"):
        select_pending(cfg, _pending())


def test_discover_pending_hits_the_real_endpoint_and_checks_identity(monkeypatch):
    cfg = _cfg(
        monkeypatch,
        ORACLE_TOKEN="tok",
        ORACLE_PRIVKEY="pk",
        ORACLE_ID="oracle-a",
        NODE_URL="http://node:8556/",
        EXPECTED_CHAIN_ID="wrf-testnet",
    )
    calls = []

    def fake_get(url, token):
        calls.append((url, token))
        return _pending()

    monkeypatch.setattr(loop, "_get_json", fake_get)
    items, _ = loop.discover_pending(cfg, None)
    assert calls == [("http://node:8556/transactions/oracle/pending?limit=100", "tok")]
    assert [i["commit_id"] for i in items] == ["c-fresh"]

    monkeypatch.setattr(loop, "_get_json", lambda url, token: _pending(chain_id="other"))
    with pytest.raises(SystemExit, match="chain_id"):
        loop.discover_pending(cfg, None)


# ------------------------------------------------------------ reference check


def test_canonical_variables_maps_core4_aliases():
    got = canonical_variables(
        {"temp_c": 21.5, "relative_humidity": 55, "pressure": 1008.2, "wind_speed": 3.0,
         "rainfall": 0.2, "dewpoint_c": 13.0, "station": "x", "flag": True}
    )
    assert got == {
        "temperature_c": 21.5,
        "humidity_pct": 55.0,
        "pressure_hpa": 1008.2,
        "wind_speed_ms": 3.0,
        "rainfall_mm": 0.2,
    }


def test_build_observation_uses_station_locations_file(monkeypatch, tmp_path):
    locations = tmp_path / "stations.json"
    locations.write_text(json.dumps({"STN1": {"lat": 46.8, "lon": -92.1, "elevation_m": 200}}))
    cfg = _cfg(
        monkeypatch, ORACLE_TOKEN="t", ORACLE_PRIVKEY="pk", STATION_LOCATIONS_FILE=str(locations)
    )
    obs = build_observation(
        cfg,
        {"commit_id": "c", "reading": {"temperature_c": 21.5}, "station_id": "STN1",
         "timestamp": 1_758_000_000, "nonce": "1"},
    )
    assert obs == {
        "station_id": "STN1",
        "lat": 46.8,
        "lon": -92.1,
        "elevation_m": 200.0,
        "observation": {"temperature_c": 21.5},
        "observed_at_unix": 1_758_000_000,
    }


def test_build_observation_prefers_coordinates_in_the_reading(monkeypatch):
    cfg = _cfg(monkeypatch, ORACLE_TOKEN="t", ORACLE_PRIVKEY="pk")
    obs = build_observation(
        cfg,
        {"commit_id": "c", "reading": {"temperature_c": 21.5, "latitude": 1.5, "longitude": 2.5},
         "station_id": "unknown", "timestamp": 5, "nonce": "1"},
    )
    assert obs["lat"] == 1.5 and obs["lon"] == 2.5


def test_build_observation_skips_without_location_or_variables(monkeypatch, capsys):
    cfg = _cfg(monkeypatch, ORACLE_TOKEN="t", ORACLE_PRIVKEY="pk")
    assert build_observation(
        cfg, {"commit_id": "c1", "reading": {"temperature_c": 1.0}, "station_id": "nowhere", "nonce": "1"}
    ) is None
    assert build_observation(
        cfg, {"commit_id": "c2", "reading": {"foo": 1.0, "lat": 0, "lon": 0}, "station_id": "s", "nonce": "1"}
    ) is None
    out = capsys.readouterr().out
    assert "no location for station 'nowhere'" in out
    assert "no canonical variables" in out


def test_decide_from_result_uses_ratio_contract():
    assert decide_from_result({"worst_ratio": 0.5}) == (True, 0.75)
    assert decide_from_result({"worst_ratio": 1.0}) == (True, 0.5)
    assert decide_from_result({"worst_ratio": 1.4}) == (False, 0.3)
    assert decide_from_result({"worst_ratio": 3.0}) == (False, 0.0)
    assert decide_from_result({"worst_ratio": None}) is None


def test_reference_check_posts_ratio_mode_to_v1_residuals(monkeypatch):
    cfg = _cfg(monkeypatch, ORACLE_TOKEN="t", ORACLE_PRIVKEY="pk", REFERENCE_URL="http://ref:8010/")
    posted = []

    def fake_post(url, token, body):
        posted.append((url, token, body))
        return 200, json.dumps(
            {"results": [{"station_id": "STN1", "per_source": [], "worst_ratio": 0.5,
                          "marginal_threshold": 1.0, "outlier_threshold": 2.0}]}
        )

    monkeypatch.setattr(loop, "_post_json", fake_post)
    item = {"commit_id": "c", "reading": {"temperature_c": 21.5, "lat": 46.8, "lon": -92.1},
            "station_id": "STN1", "timestamp": 1_758_000_000, "nonce": "1"}
    assert reference_check(cfg, item) == (True, 0.75)
    url, token, body = posted[0]
    assert url == "http://ref:8010/v1/residuals" and token == ""
    assert body["mode"] == "ratio"
    assert body["observations"][0]["observation"] == {"temperature_c": 21.5}
    assert body["observations"][0]["lat"] == 46.8


def test_reference_check_requires_reference_url(monkeypatch):
    cfg = _cfg(monkeypatch, ORACLE_TOKEN="t", ORACLE_PRIVKEY="pk", REFERENCE_URL="")
    with pytest.raises(SystemExit, match="REFERENCE_URL unset"):
        reference_check(cfg, {"commit_id": "c", "reading": {}, "station_id": "s", "nonce": "1"})


def test_unsafe_approve_all_needs_env_gate_and_short_circuits(monkeypatch):
    monkeypatch.delenv("ATTESTATION_LOOP_ALLOW_UNSAFE", raising=False)
    with pytest.raises(SystemExit, match="ATTESTATION_LOOP_ALLOW_UNSAFE"):
        _cfg(monkeypatch, unsafe_approve_all=True, ORACLE_TOKEN="t", ORACLE_PRIVKEY="pk")
    cfg = _cfg(
        monkeypatch, unsafe_approve_all=True, ORACLE_TOKEN="t", ORACLE_PRIVKEY="pk",
        ATTESTATION_LOOP_ALLOW_UNSAFE="1",
    )
    assert reference_check(cfg, {"commit_id": "c", "reading": {}, "station_id": "s", "nonce": "1"}) == (
        True, 0.75,
    )


# ------------------------------------------------------------- sign + submit


def test_sign_attestation_shells_out_to_signer(monkeypatch, tmp_path):
    # Fake signer stands in for the Rust oracle_attest_sign: echo a body that
    # reflects the args, proving the subprocess wiring + arg order.
    fake = tmp_path / "fake_signer.sh"
    fake.write_text(
        '#!/usr/bin/env bash\n'
        'printf \'{"oracle_id":"OID","commit_hash":"%s","approved":%s,'
        '"confidence":%s,"timestamp":%s,"nonce":"%s","signature":"sig"}\' '
        '"$2" "$3" "$4" "$5" "$6"\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    cfg = _cfg(monkeypatch, ORACLE_TOKEN="t", ORACLE_PRIVKEY="pk", SIGNER_BIN=str(fake))
    body = sign_attestation(cfg, "commit-1", True, 0.9, "7")
    assert body["commit_hash"] == "commit-1"
    assert body["approved"] is True  # emitted as a JSON bool, like the real signer
    assert body["nonce"] == "7"
    assert body["signature"] == "sig"
    assert json.dumps(body)  # round-trips


def test_run_once_is_dry_run_unless_submit(monkeypatch, capsys):
    cfg = _cfg(monkeypatch, ORACLE_TOKEN="t", ORACLE_PRIVKEY="pk", ORACLE_ID="oracle-a",
               REFERENCE_URL="http://ref")
    monkeypatch.setattr(loop, "oracle_status", lambda cfg: {"oracle_set": ["oracle-a"], "oracle_quorum": 1})
    monkeypatch.setattr(loop, "discover_pending", lambda cfg, manual: (
        [{"commit_id": "c", "reading": {"temperature_c": 1.0, "lat": 0, "lon": 0}, "station_id": "s",
          "timestamp": 1, "nonce": "1"}], []))
    monkeypatch.setattr(loop, "reference_check", lambda cfg, item: (True, 0.9))
    monkeypatch.setattr(loop, "sign_attestation", lambda *a: {"signature": "sig"})
    posts = []
    monkeypatch.setattr(loop, "_post_json", lambda url, token, body: posts.append(url) or (200, "{}"))

    assert loop.run_once(cfg, None) == 0
    assert posts == []
    assert "[dry-run] would attest c" in capsys.readouterr().out

    cfg_submit = _cfg(monkeypatch, submit=True, ORACLE_TOKEN="t", ORACLE_PRIVKEY="pk",
                      ORACLE_ID="oracle-a", REFERENCE_URL="http://ref")
    assert loop.run_once(cfg_submit, None) == 1
    assert posts == ["http://localhost:8556/transactions/oracle/attestation"]


def test_manual_pending_is_explicit_local_testing_only(monkeypatch):
    cfg = _cfg(monkeypatch, ORACLE_TOKEN="t", ORACLE_PRIVKEY="pk")
    monkeypatch.setattr(loop, "_get_json", lambda *a: (_ for _ in ()).throw(AssertionError("node hit")))
    items, skips = loop.discover_pending(
        cfg, [{"commit_id": "m1", "reading": {"temperature_c": 1.0}, "nonce": 3}]
    )
    assert skips == [] and items[0]["commit_id"] == "m1" and items[0]["nonce"] == "3"
