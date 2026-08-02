import argparse
import json
import stat

import pytest

from agent.attestation_loop import (
    Config,
    ConfigurationError,
    ReferenceError,
    load_pending,
    reference_check,
    sign_attestation,
)


def _args() -> argparse.Namespace:
    return argparse.Namespace(interval=60.0, dry_run=False)


def test_config_fails_closed_without_injected_runtime_inputs(monkeypatch):
    for name in ("NODE_URL", "ORACLE_TOKEN", "ORACLE_PRIVKEY", "REFERENCE_URL", "SIGNER_BIN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigurationError, match="NODE_URL"):
        Config.from_env(_args()).validate()


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
