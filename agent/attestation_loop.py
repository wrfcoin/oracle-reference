"""Reference-checked oracle attestation loop.

One oracle operator runs this. Each cycle it:
  1. polls the node's ``/transactions/oracle/status`` for pending commits,
  2. resolves each pending commit's reading (DISCOVERY — see note below),
  3. reference-checks the reading against the earth2studio reference-verifier
     (``/v1/residuals``) to decide ``approved`` + ``confidence``,
  4. signs the attestation with the CORRECT protocol crypto by shelling out to
     the Rust ``oracle_attest_sign`` tool (guaranteed to match the node
     verifier — we never re-implement the signing in Python), and
  5. submits it to ``/transactions/oracle/attestation`` (Oracle-role token).

Two upstream dependencies are not yet available and are isolated behind the
``discover_pending`` and ``reference_check`` hooks so this harness is complete
and testable today:

  * DISCOVERY: there is no REST endpoint that lists pending commits *with their
    readings*. Until one exists, feed commit readings via ``--manual`` (a JSON
    file of {commit_id, reading, nonce}) or wire ``discover_pending`` to your
    reveal source.
  * REFERENCE CHECK: ``/v1/residuals`` is oracle-reference Wave-2. Until it
    ships, ``--approve-all`` attests everything at a fixed confidence (testnet
    smoke only) — do NOT use ``--approve-all`` for real reward-affecting runs.

Env:
  NODE_URL          e.g. http://localhost:8556
  ORACLE_TOKEN      Oracle-role bearer token
  ORACLE_PRIVKEY    the oracle's ed25519 privkey hex (from oracle_keygen)
  REFERENCE_URL     e.g. http://wrfcoin-faucet-reference:8010  (Wave-2)
  SIGNER_BIN        path to the built oracle_attest_sign binary
                    (cargo build -p wrfcoin-shared-protocol --example oracle_attest_sign)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class Config:
    node_url: str
    oracle_token: str
    oracle_privkey: str
    reference_url: str
    signer_bin: str
    interval_s: float
    approve_all: bool
    dry_run: bool

    @classmethod
    def from_env(cls, args: argparse.Namespace) -> "Config":
        return cls(
            node_url=os.environ.get("NODE_URL", "http://localhost:8556").rstrip("/"),
            oracle_token=os.environ.get("ORACLE_TOKEN", ""),
            oracle_privkey=os.environ.get("ORACLE_PRIVKEY", ""),
            reference_url=os.environ.get("REFERENCE_URL", "").rstrip("/"),
            signer_bin=os.environ.get("SIGNER_BIN", "oracle_attest_sign"),
            interval_s=args.interval,
            approve_all=args.approve_all,
            dry_run=args.dry_run,
        )


def _get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (trusted node)
        return json.loads(resp.read().decode())


def _post_json(url: str, token: str, body: dict) -> tuple[int, str]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        return e.code, e.read().decode()


def oracle_status(cfg: Config) -> dict:
    return _get_json(f"{cfg.node_url}/transactions/oracle/status", cfg.oracle_token)


def discover_pending(cfg: Config, manual: list[dict] | None) -> list[dict]:
    """Return pending commits as [{commit_id, reading, nonce}].

    DISCOVERY dependency: no endpoint lists pending commits with readings yet.
    ``--manual <file>`` supplies them; otherwise wire this to your reveal
    source (revealed ContributionReveal txs carry the reading + commit_id).
    """
    if manual is not None:
        return manual
    # TODO(#724): read revealed contributions (commit_id + reading) from the
    # chain / a contributions endpoint once available.
    return []


def reference_check(cfg: Config, reading: dict) -> tuple[bool, float]:
    """Return (approved, confidence) for a reading.

    REFERENCE dependency: posts the reading to the reference-verifier's
    ``/v1/residuals`` (Wave-2) and derives approval from the residual bands.
    Until that endpoint exists, ``--approve-all`` short-circuits to a fixed
    verdict for testnet smoke only.
    """
    if cfg.approve_all:
        return True, 0.75
    if not cfg.reference_url:
        raise SystemExit(
            "REFERENCE_URL unset and --approve-all not given: cannot decide "
            "attestations without the reference-verifier (/v1/residuals, Wave-2)."
        )
    status, body = _post_json(
        f"{cfg.reference_url}/v1/residuals",
        token="",
        body={"observations": [reading]},
    )
    if status != 200:
        raise RuntimeError(f"reference-verifier /v1/residuals returned {status}: {body[:200]}")
    result = json.loads(body)["results"][0]
    # Approve when no per-source residual exceeds its Marginal band; confidence
    # scales with the best-source agreement. (Band verdicts come from core4's
    # reference.rs via the service; this is the operator-side gate.)
    worst = max((abs(r) for src in result["per_source"] for r in src["residuals"].values()), default=0.0)
    approved = worst <= result.get("marginal_threshold", float("inf"))
    confidence = max(0.0, min(1.0, 1.0 - worst / (result.get("outlier_threshold", 1.0) or 1.0)))
    return approved, round(confidence, 3)


def sign_attestation(cfg: Config, commit_id: str, approved: bool, confidence: float, nonce: str) -> dict:
    """Sign via the Rust oracle_attest_sign tool (correct, matches the node)."""
    out = subprocess.run(
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
    return json.loads(out.stdout.strip())


def run_once(cfg: Config, manual: list[dict] | None) -> int:
    st = oracle_status(cfg)
    pending = st.get("commits_pending_quorum", 0)
    print(f"[status] oracle_set={len(st.get('oracle_set', []))} quorum={st.get('oracle_quorum')} "
          f"pending={pending} with_quorum={st.get('commits_with_quorum')}")
    submitted = 0
    for item in discover_pending(cfg, manual):
        commit_id = item["commit_id"]
        nonce = str(item.get("nonce", int(time.time())))
        approved, confidence = reference_check(cfg, item["reading"])
        body = sign_attestation(cfg, commit_id, approved, confidence, nonce)
        if cfg.dry_run:
            print(f"[dry-run] would attest {commit_id}: approved={approved} confidence={confidence}")
            continue
        status, resp = _post_json(
            f"{cfg.node_url}/transactions/oracle/attestation", cfg.oracle_token, body
        )
        ok = 200 <= status < 300
        submitted += ok
        print(f"[attest] {commit_id} approved={approved} conf={confidence} -> {status} {'OK' if ok else resp[:160]}")
    return submitted


def main() -> None:
    p = argparse.ArgumentParser(description="Reference-checked oracle attestation loop")
    p.add_argument("--once", action="store_true", help="run one cycle and exit")
    p.add_argument("--interval", type=float, default=60.0, help="seconds between cycles")
    p.add_argument("--manual", type=str, help="JSON file of [{commit_id, reading, nonce}] (discovery stand-in)")
    p.add_argument("--approve-all", action="store_true", help="TESTNET SMOKE ONLY: skip reference check")
    p.add_argument("--dry-run", action="store_true", help="sign but do not submit")
    args = p.parse_args()

    cfg = Config.from_env(args)
    if not cfg.oracle_privkey or not cfg.oracle_token:
        raise SystemExit("ORACLE_PRIVKEY and ORACLE_TOKEN must be set")
    manual = json.load(open(args.manual)) if args.manual else None

    if args.once:
        run_once(cfg, manual)
        return
    while True:
        try:
            run_once(cfg, manual)
        except Exception as e:  # keep the loop alive across transient errors
            print(f"[error] cycle failed: {e}")
        time.sleep(cfg.interval_s)


if __name__ == "__main__":
    main()
