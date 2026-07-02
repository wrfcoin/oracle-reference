import argparse
import json
import os
import stat

from agent.attestation_loop import Config, reference_check, sign_attestation


def _cfg(**over):
    args = argparse.Namespace(interval=60.0, approve_all=over.pop("approve_all", False), dry_run=False)
    for k, v in over.items():
        os.environ[k] = v
    return Config.from_env(args)


def test_approve_all_short_circuits_reference():
    cfg = _cfg(approve_all=True, ORACLE_TOKEN="t", ORACLE_PRIVKEY="deadbeef")
    approved, confidence = reference_check(cfg, {"station_id": "x"})
    assert approved is True
    assert 0.0 <= confidence <= 1.0


def test_reference_check_requires_a_source_when_not_approve_all():
    cfg = _cfg(approve_all=False, ORACLE_TOKEN="t", ORACLE_PRIVKEY="d", REFERENCE_URL="")
    try:
        reference_check(cfg, {"station_id": "x"})
        raise AssertionError("expected SystemExit when no reference source and not --approve-all")
    except SystemExit:
        pass


def test_sign_attestation_shells_out_to_signer(tmp_path):
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
    cfg = _cfg(ORACLE_TOKEN="t", ORACLE_PRIVKEY="pk", SIGNER_BIN=str(fake))
    body = sign_attestation(cfg, "commit-1", True, 0.9, "7")
    assert body["commit_hash"] == "commit-1"
    assert body["approved"] is True  # emitted as a JSON bool, like the real signer
    assert body["nonce"] == "7"
    assert body["signature"] == "sig"
    assert json.dumps(body)  # round-trips
