import argparse
import io
import json
import stat
from urllib.error import HTTPError

import pytest

import agent.attestation_loop as loop
from agent.attestation_loop import (
    Config,
    ConfigurationError,
    ReferenceError,
    load_pending,
    reference_check,
    run_once,
    sign_attestation,
)


def _args() -> argparse.Namespace:
    return argparse.Namespace(interval=60.0, dry_run=False)


def test_config_fails_closed_without_injected_runtime_inputs(monkeypatch):
    for name in ("NODE_URL", "ORACLE_TOKEN", "ORACLE_PRIVKEY", "REFERENCE_URL", "SIGNER_BIN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigurationError, match="NODE_URL"):
        Config.from_env(_args()).validate()


def test_run_once_requires_nonempty_token_before_network(monkeypatch):
    cfg = Config("node", "   ", "private", "reference", "signer", 60.0, False)
    network_called = False

    def unexpected_network(_cfg):
        nonlocal network_called
        network_called = True
        raise AssertionError("network access occurred before token validation")

    monkeypatch.setattr("agent.attestation_loop.oracle_status", unexpected_network)
    with pytest.raises(ConfigurationError, match="ORACLE_TOKEN"):
        run_once(cfg, [])
    assert network_called is False


def test_pending_input_requires_commit_reading_and_nonce(tmp_path):
    path = tmp_path / "pending.json"
    path.write_text(json.dumps([{"commit_id": "c1", "reading": {"lat": 1}}]))
    with pytest.raises(ConfigurationError, match="nonce"):
        load_pending(str(path))


def test_reference_check_fails_closed_on_incomplete_result(monkeypatch):
    monkeypatch.setattr(
        "agent.attestation_loop._post_json",
        lambda _url, _body: (200, json.dumps({"results": [{"per_source": []}]})),
    )
    cfg = Config("node", "token", "private", "reference", "signer", 60.0, False)
    with pytest.raises(ReferenceError, match="incomplete"):
        reference_check(cfg, {"lat": 1})


def test_reference_check_uses_ratio_result(monkeypatch):
    monkeypatch.setattr(
        "agent.attestation_loop._post_json",
        lambda _url, body: (
            200,
            json.dumps(
                {
                    "results": [
                        {
                            "per_source": [{"source": "hrrr", "band_ratios": {"temperature_c": 0.2}}],
                            "worst_ratio": 0.2,
                            "marginal_threshold": 1.0,
                            "outlier_threshold": 2.0,
                        }
                    ]
                }
            ),
        ),
    )
    cfg = Config("node", "token", "private", "reference", "signer", 60.0, False)
    assert reference_check(cfg, {"lat": 1}) == (True, 0.9)


def test_sign_attestation_shells_out_to_protocol_signer(tmp_path):
    fake = tmp_path / "signer.sh"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "printf '{\"commit_hash\":\"%s\",\"approved\":%s,\"confidence\":%s,\"nonce\":\"%s\"}' \"$2\" \"$3\" \"$4\" \"$6\"\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    cfg = Config("node", "token", "private", "reference", str(fake), 60.0, False)
    body = sign_attestation(cfg, "commit-1", True, 0.9, "7")
    assert body == {"commit_hash": "commit-1", "approved": True, "confidence": 0.9, "nonce": "7"}


def test_attestation_submit_sends_bearer_and_redacts_token_from_error(monkeypatch):
    token = "oracle-token-that-must-not-leak"
    cfg = Config("http://node", token, "private", "reference", "signer", 60.0, False)
    seen = {}

    monkeypatch.setattr(loop, "oracle_status", lambda _cfg: {"commits_pending_quorum": 1})
    monkeypatch.setattr(loop, "reference_check", lambda _cfg, _reading: (True, 0.9))
    monkeypatch.setattr(
        loop,
        "sign_attestation",
        lambda _cfg, _commit_id, _approved, _confidence, _nonce: {"signed": True},
    )

    def reject_submit(request, timeout):
        seen["authorization"] = request.get_header("Authorization")
        raise HTTPError(
            request.full_url,
            403,
            "forbidden",
            hdrs=None,
            fp=io.BytesIO(f"echoed credential: {token}".encode()),
        )

    monkeypatch.setattr(loop.urllib.request, "urlopen", reject_submit)
    with pytest.raises(RuntimeError) as error:
        run_once(cfg, [{"commit_id": "c1", "reading": {"lat": 1}, "nonce": "7"}])

    assert seen["authorization"] == f"Bearer {token}"
    assert token not in str(error.value)
    assert "[REDACTED]" in str(error.value)
