"""Fail-closed, operator-side oracle attestation seam.

This process deliberately does not belong in the backend.  An operator runs
one instance per registered oracle and injects:

* the node URL, Oracle-role bearer token, and oracle private key;
* an independent ``oracle-reference`` URL; and
* a pending-reading JSON file supplied by the operator's chain/reveal
  collector until core4 exposes a read endpoint for reveal readings.

The private key is passed only to the protocol's Rust signer executable.  The
Python seam never creates keys, tokens, reference values, or signatures.  It
refuses to run when any required input is absent or when the reference service
does not return a complete, finite result.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigurationError(RuntimeError):
    """The operator has not injected the complete runtime envelope."""


class ReferenceError(RuntimeError):
    """The independent reference service did not produce a usable result."""


@dataclass(frozen=True)
class Config:
    node_url: str
    oracle_token: str
    oracle_privkey: str
    reference_url: str
    signer_bin: str
    interval_s: float
    dry_run: bool

    @classmethod
    def from_env(cls, args: argparse.Namespace) -> "Config":
        return cls(
            node_url=os.environ.get("NODE_URL", "").rstrip("/"),
            oracle_token=os.environ.get("ORACLE_TOKEN", ""),
            oracle_privkey=os.environ.get("ORACLE_PRIVKEY", ""),
            reference_url=os.environ.get("REFERENCE_URL", "").rstrip("/"),
            signer_bin=os.environ.get("SIGNER_BIN", ""),
            interval_s=args.interval,
            dry_run=args.dry_run,
        )

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("NODE_URL", self.node_url),
                ("ORACLE_TOKEN", self.oracle_token),
                ("ORACLE_PRIVKEY", self.oracle_privkey),
                ("REFERENCE_URL", self.reference_url),
                ("SIGNER_BIN", self.signer_bin),
            )
            if not value.strip()
        ]
        if missing:
            raise ConfigurationError(
                "required injected runtime input(s) missing: " + ", ".join(missing)
            )
        if self.interval_s <= 0:
            raise ConfigurationError("--interval must be greater than zero")


def _get_json(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        payload = json.loads(response.read().decode())
    if not isinstance(payload, dict):
        raise RuntimeError("node returned a non-object JSON response")
    return payload


def _require_oracle_token(token: str) -> str:
    if not token.strip():
        raise ConfigurationError("ORACLE_TOKEN must be injected and non-empty")
    return token


def _redact_secret(text: str, secret: str | None) -> str:
    if not secret:
        return text
    return text.replace(secret, "[REDACTED]")


def _post_json(
    url: str, body: dict[str, Any], *, token: str | None = None
) -> tuple[int, str]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {_require_oracle_token(token)}"
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, _redact_secret(error.read().decode(), token)


def oracle_status(cfg: Config) -> dict[str, Any]:
    return _get_json(f"{cfg.node_url}/transactions/oracle/status", cfg.oracle_token)


def load_pending(path: str) -> list[dict[str, Any]]:
    """Load reveal readings injected by the operator's chain/reveal collector."""
    try:
        payload = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"cannot read pending input file: {path}") from error
    if not isinstance(payload, list) or not payload:
        raise ConfigurationError("pending input file must be a non-empty JSON array")

    pending: list[dict[str, Any]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ConfigurationError(f"pending item {index} is not an object")
        if not isinstance(item.get("commit_id"), (str, int)):
            raise ConfigurationError(f"pending item {index} has no commit_id")
        if not isinstance(item.get("reading"), dict) or not item["reading"]:
            raise ConfigurationError(f"pending item {index} has no independent reading")
        if not isinstance(item.get("nonce"), (str, int)):
            raise ConfigurationError(f"pending item {index} has no nonce")
        pending.append(item)
    return pending


def reference_check(cfg: Config, reading: dict[str, Any]) -> tuple[bool, float]:
    """Return an approval only from a complete independent residual result."""
    status, body = _post_json(
        f"{cfg.reference_url}/v1/residuals",
        {"observations": [reading], "mode": "ratio"},
    )
    if status != 200:
        raise ReferenceError(
            f"reference-verifier /v1/residuals returned HTTP {status}: {body[:200]}"
        )
    try:
        response = json.loads(body)
        results = response["results"]
        result = results[0]
        worst_ratio = result["worst_ratio"]
        marginal = result["marginal_threshold"]
        outlier = result["outlier_threshold"]
        per_source = result["per_source"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise ReferenceError("reference-verifier returned an incomplete result") from error

    if not isinstance(per_source, list) or not per_source:
        raise ReferenceError("reference-verifier returned no source results")
    numbers = (worst_ratio, marginal, outlier)
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in numbers):
        raise ReferenceError("reference-verifier returned non-finite thresholds")
    if outlier <= 0 or marginal < 0 or marginal > outlier:
        raise ReferenceError("reference-verifier returned invalid thresholds")

    approved = worst_ratio <= marginal
    confidence = max(0.0, min(1.0, 1.0 - worst_ratio / outlier))
    return approved, round(confidence, 3)


def sign_attestation(
    cfg: Config,
    commit_id: str,
    approved: bool,
    confidence: float,
    nonce: str,
) -> dict[str, Any]:
    """Use the protocol signer; never reimplement signing in Python."""
    completed = subprocess.run(
        [
            cfg.signer_bin,
            cfg.oracle_privkey,
            commit_id,
            "true" if approved else "false",
            str(confidence),
            str(int(time.time())),
            nonce,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    body = json.loads(completed.stdout.strip())
    if not isinstance(body, dict):
        raise RuntimeError("protocol signer returned a non-object JSON response")
    return body


def run_once(cfg: Config, pending: list[dict[str, Any]]) -> int:
    _require_oracle_token(cfg.oracle_token)
    status = oracle_status(cfg)
    if int(status.get("commits_pending_quorum", 0)) <= 0:
        return 0

    submitted = 0
    for item in pending:
        commit_id = str(item["commit_id"])
        approved, confidence = reference_check(cfg, item["reading"])
        body = sign_attestation(
            cfg, commit_id, approved, confidence, str(item["nonce"])
        )
        if cfg.dry_run:
            continue
        code, response = _post_json(
            f"{cfg.node_url}/transactions/oracle/attestation",
            body,
            token=cfg.oracle_token,
        )
        if not 200 <= code < 300:
            raise RuntimeError(
                f"oracle attestation rejected for commit {commit_id}: HTTP {code}: "
                f"{response[:200]}"
            )
        submitted += 1
    return submitted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pending-file", required=True, help="injected reveal-reading JSON array")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--dry-run", action="store_true", help="sign but do not submit")
    args = parser.parse_args()

    cfg = Config.from_env(args)
    cfg.validate()
    if args.once:
        run_once(cfg, load_pending(args.pending_file))
        return
    while True:
        try:
            # Reload the operator-owned handoff each cycle so newly revealed
            # readings are observed without restarting the scheduler.
            run_once(cfg, load_pending(args.pending_file))
        except Exception as error:  # keep a healthy loop alive across transient failures
            print(f"[error] attestation cycle failed: {error}")
        time.sleep(cfg.interval_s)


if __name__ == "__main__":
    main()
