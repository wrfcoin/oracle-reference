"""Reference-checked oracle attestation loop.

One oracle operator runs this. Each cycle it:
  1. discovers pending commits from the node's ``GET /transactions/oracle/pending``
     (core4#2464: the attester read surface — carries the full reveal preimage,
     the attestation state, and the node's chain identity),
  2. skips anything already at quorum or already attested by this oracle,
  3. reference-checks each reading against the earth2studio reference-verifier
     (``POST /v1/residuals``, oracle-reference Wave-2) to decide ``approved`` +
     ``confidence`` from the dimensionless band ratio,
  4. signs the attestation with the CORRECT protocol crypto by shelling out to
     the Rust ``oracle_attest_sign`` tool (guaranteed to match the node
     verifier — we never re-implement the signing in Python), and
  5. submits it to ``/transactions/oracle/attestation`` (Oracle-role token) —
     ONLY when ``--submit`` is given. The default is dry-run.

Shadow mode (oracle-reference README, Phase 0): this repo's scores have zero
reward impact until the Phase 0.5 enforcement flip is human-approved. An
attestation, once submitted, DOES count toward the node's oracle quorum and
therefore toward contributor rewards. So the loop's default is ``--dry-run``
semantics (discover, check, sign, print) and submission requires the explicit
``--submit`` flag: making the automation real (wrfcoin/infra#945, story
oracle-reference#8) is not the same act as flipping that switch.

Testnet-smoke escape hatches, both loud and never defaults:
  * ``--unsafe-approve-all`` (also needs ``ATTESTATION_LOOP_ALLOW_UNSAFE=1``):
    attest everything at a fixed confidence without a reference check.
  * ``--manual-pending FILE``: JSON list of ``{commit_id, reading, nonce,
    station_id?, timestamp?}`` used instead of the node's pending endpoint,
    for local testing without a node.

Station locations: ``/transactions/oracle/pending`` carries the reading but
not the station's coordinates, and ``/v1/residuals`` needs lat/lon. They are
resolved, in order, from the reading itself (``lat``/``lon`` or
``latitude``/``longitude`` keys) or from ``STATION_LOCATIONS_FILE`` (JSON map
``{station_id: {lat, lon, elevation_m?}}``, the same shape backend's
``GET /api/weather/stations`` rows can be reduced to). A commit whose station
has no known location is SKIPPED with a printed reason — never approved
blind.

Env:
  NODE_URL                e.g. http://localhost:8556
  ORACLE_TOKEN            Oracle-role bearer token (also satisfies the Reader
                          role /transactions/oracle/pending requires)
  ORACLE_PRIVKEY          the oracle's ed25519 privkey hex (from oracle_keygen)
  ORACLE_ID               this oracle's id as it appears in the node's
                          oracle_set; used to skip commits already attested by
                          us. Optional but strongly recommended (without it the
                          self-skip is disabled and a restart may double-attest
                          until the node's attesting_oracles catches it).
  REFERENCE_URL           e.g. http://wrfcoin-faucet-reference:8010
  SIGNER_BIN              path to the built oracle_attest_sign binary
                          (cargo build -p wrfcoin-shared-protocol --example oracle_attest_sign)
  STATION_LOCATIONS_FILE  optional JSON map station_id -> {lat, lon, elevation_m}
  EXPECTED_CHAIN_ID / EXPECTED_NETWORK_ID
                          optional; if set, refuse to attest for a node whose
                          /pending response reports a different identity
                          (core4#2713/#2744 trap class).
  ATTESTATION_LOOP_ALLOW_UNSAFE=1
                          required alongside --unsafe-approve-all.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# Ratio-space contract of /v1/residuals mode="ratio" (app/services/residuals.py):
# worst_ratio <= 1.0 is inside every source's Marginal band; 2.0 is the
# confidence-scaling constant (confidence 0.5 at the marginal edge, 0 at 2x).
RATIO_MARGINAL_THRESHOLD = 1.0
RATIO_OUTLIER_THRESHOLD = 2.0

# core4 canonical reading keys (shared-protocol weather/validation.rs accepts
# these aliases) -> oracle-reference canonical variables (app/bands.py).
_VARIABLE_ALIASES: dict[str, str] = {
    "temperature_c": "temperature_c",
    "temperature": "temperature_c",
    "temp_c": "temperature_c",
    "humidity_pct": "humidity_pct",
    "humidity": "humidity_pct",
    "relative_humidity": "humidity_pct",
    "pressure_hpa": "pressure_hpa",
    "pressure": "pressure_hpa",
    "wind_speed_ms": "wind_speed_ms",
    "wind_speed": "wind_speed_ms",
    "rainfall_mm": "rainfall_mm",
    "rainfall": "rainfall_mm",
    "precipitation": "rainfall_mm",
}

PENDING_PAGE_LIMIT = 100


@dataclass
class Config:
    node_url: str
    oracle_token: str
    oracle_privkey: str
    oracle_id: str
    reference_url: str
    signer_bin: str
    interval_s: float
    unsafe_approve_all: bool
    submit: bool
    station_locations: dict[str, dict[str, float]] = field(default_factory=dict)
    expected_chain_id: str = ""
    expected_network_id: str = ""

    @classmethod
    def from_env(cls, args: argparse.Namespace) -> Config:
        unsafe = bool(getattr(args, "unsafe_approve_all", False))
        if unsafe and os.environ.get("ATTESTATION_LOOP_ALLOW_UNSAFE") != "1":
            raise SystemExit(
                "--unsafe-approve-all is a testnet-smoke escape hatch: it also needs "
                "ATTESTATION_LOOP_ALLOW_UNSAFE=1 in the environment."
            )
        locations_file = os.environ.get("STATION_LOCATIONS_FILE", "")
        locations = load_station_locations(locations_file) if locations_file else {}
        return cls(
            node_url=os.environ.get("NODE_URL", "http://localhost:8556").rstrip("/"),
            oracle_token=os.environ.get("ORACLE_TOKEN", ""),
            oracle_privkey=os.environ.get("ORACLE_PRIVKEY", ""),
            oracle_id=os.environ.get("ORACLE_ID", ""),
            reference_url=os.environ.get("REFERENCE_URL", "").rstrip("/"),
            signer_bin=os.environ.get("SIGNER_BIN", "oracle_attest_sign"),
            interval_s=args.interval,
            unsafe_approve_all=unsafe,
            submit=bool(getattr(args, "submit", False)),
            station_locations=locations,
            expected_chain_id=os.environ.get("EXPECTED_CHAIN_ID", ""),
            expected_network_id=os.environ.get("EXPECTED_NETWORK_ID", ""),
        )


def load_station_locations(path: str) -> dict[str, dict[str, float]]:
    """``{station_id: {lat, lon, elevation_m?}}`` — validated, so a typo in the
    file fails at startup instead of silently skipping every commit."""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise SystemExit(f"{path}: expected a JSON object keyed by station_id")
    out: dict[str, dict[str, float]] = {}
    for station_id, loc in raw.items():
        try:
            entry = {"lat": float(loc["lat"]), "lon": float(loc["lon"])}
        except (KeyError, TypeError, ValueError) as exc:
            raise SystemExit(f"{path}: station {station_id!r} needs numeric lat/lon") from exc
        if loc.get("elevation_m") is not None:
            entry["elevation_m"] = float(loc["elevation_m"])
        out[str(station_id)] = entry
    return out


def _get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _post_json(url: str, token: str, body: dict) -> tuple[int, str]:
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        return e.code, e.read().decode()


def oracle_status(cfg: Config) -> dict:
    return _get_json(f"{cfg.node_url}/transactions/oracle/status", cfg.oracle_token)


# ----------------------------------------------------------------- discovery


def fetch_pending(cfg: Config) -> dict:
    """Raw ``GET /transactions/oracle/pending`` response (core4#2464)."""
    return _get_json(
        f"{cfg.node_url}/transactions/oracle/pending?limit={PENDING_PAGE_LIMIT}",
        cfg.oracle_token,
    )


def check_node_identity(cfg: Config, pending: dict) -> None:
    """Refuse to attest for a node whose chain identity is not the one we were
    configured for (core4#2713/#2744: a silent mismatch otherwise surfaces only
    as opaque signature rejections)."""
    if cfg.expected_chain_id and pending.get("chain_id") != cfg.expected_chain_id:
        raise SystemExit(
            f"node chain_id {pending.get('chain_id')!r} != EXPECTED_CHAIN_ID {cfg.expected_chain_id!r}"
        )
    if cfg.expected_network_id and pending.get("network_id") != cfg.expected_network_id:
        raise SystemExit(
            f"node network_id {pending.get('network_id')!r} != EXPECTED_NETWORK_ID "
            f"{cfg.expected_network_id!r}"
        )


def select_pending(cfg: Config, pending: dict) -> tuple[list[dict], list[tuple[str, str]]]:
    """Turn the node's response into work items ``[{commit_id, reading, nonce,
    station_id, timestamp}]`` plus a list of ``(commit_id, reason)`` skips.

    Mirrors core4's own ``oracle_attester`` skip logic: quorum already
    reached, reward already recorded/finalized, or this oracle already in
    ``attesting_oracles`` (mined OR still in the mempool — that half is what
    stops a restarted daemon from double-attesting).
    """
    items: list[dict] = []
    skipped: list[tuple[str, str]] = []
    if cfg.oracle_id and cfg.oracle_id not in pending.get("oracle_set", []):
        raise SystemExit(
            f"ORACLE_ID {cfg.oracle_id!r} is not in the node's oracle_set "
            f"{pending.get('oracle_set')}; attestations would be rejected"
        )
    for obs in pending.get("observations", []):
        commit_id = obs["commit_id"]
        if obs.get("has_quorum"):
            skipped.append((commit_id, "already at quorum"))
            continue
        if obs.get("reward_recorded") or obs.get("reward_finalized"):
            skipped.append((commit_id, "reward already settled"))
            continue
        if cfg.oracle_id and cfg.oracle_id in obs.get("attesting_oracles", []):
            skipped.append((commit_id, "already attested by this oracle"))
            continue
        items.append(
            {
                "commit_id": commit_id,
                "reading": obs.get("data") or {},
                "nonce": str(obs["nonce"]),
                "station_id": obs.get("station_id", ""),
                "timestamp": obs.get("timestamp"),
            }
        )
    for commit_id in pending.get("unrecoverable", []):
        skipped.append((commit_id, "node reports reveal payload unrecoverable"))
    return items, skipped


def discover_pending(cfg: Config, manual: list[dict] | None) -> tuple[list[dict], list[tuple[str, str]]]:
    """Return ``(work_items, skips)``. ``manual`` (``--manual-pending``) bypasses
    the node entirely and is for local testing only."""
    if manual is not None:
        return [
            {
                "commit_id": item["commit_id"],
                "reading": item["reading"],
                "nonce": str(item.get("nonce", int(time.time()))),
                "station_id": item.get("station_id", ""),
                "timestamp": item.get("timestamp"),
            }
            for item in manual
        ], []
    pending = fetch_pending(cfg)
    check_node_identity(cfg, pending)
    return select_pending(cfg, pending)


# ----------------------------------------------------------- reference check


def canonical_variables(reading: dict[str, Any]) -> dict[str, float]:
    """Map core4's canonical reading keys (and accepted aliases) onto
    oracle-reference's canonical variable names; unknown keys are ignored."""
    out: dict[str, float] = {}
    for key, value in reading.items():
        target = _VARIABLE_ALIASES.get(key)
        if target is None or target in out:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        out[target] = float(value)
    return out


def resolve_location(cfg: Config, item: dict) -> dict[str, float] | None:
    reading = item.get("reading") or {}
    lat = reading.get("lat", reading.get("latitude"))
    lon = reading.get("lon", reading.get("longitude"))
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        loc = {"lat": float(lat), "lon": float(lon)}
        elev = reading.get("elevation_m", reading.get("elevation"))
        if isinstance(elev, (int, float)):
            loc["elevation_m"] = float(elev)
        return loc
    return cfg.station_locations.get(item.get("station_id", ""))


def build_observation(cfg: Config, item: dict) -> dict | None:
    """The ``Observation`` body /v1/residuals expects, or None (with the reason
    printed) when the commit cannot be reference-checked honestly."""
    variables = canonical_variables(item.get("reading") or {})
    if not variables:
        print(f"[skip] {item['commit_id']}: reading carries no canonical variables")
        return None
    location = resolve_location(cfg, item)
    if location is None:
        print(
            f"[skip] {item['commit_id']}: no location for station {item.get('station_id')!r} "
            "(add it to STATION_LOCATIONS_FILE)"
        )
        return None
    observation: dict[str, Any] = {
        "station_id": item.get("station_id") or None,
        "lat": location["lat"],
        "lon": location["lon"],
        "observation": variables,
    }
    if "elevation_m" in location:
        observation["elevation_m"] = location["elevation_m"]
    if item.get("timestamp") is not None:
        observation["observed_at_unix"] = int(item["timestamp"])
    else:
        observation["observed_at_unix"] = int(time.time())
    return observation


def decide_from_result(result: dict) -> tuple[bool, float] | None:
    """Ratio-mode decision (the stable dimensionless contract). None when the
    verifier could not score the reading (no reference source / no banded
    variable) — the loop must NOT attest on that."""
    worst = result.get("worst_ratio")
    if worst is None:
        return None
    approved = float(worst) <= RATIO_MARGINAL_THRESHOLD
    confidence = max(0.0, min(1.0, 1.0 - float(worst) / RATIO_OUTLIER_THRESHOLD))
    return approved, round(confidence, 3)


def reference_check(cfg: Config, item: dict) -> tuple[bool, float] | None:
    """Return ``(approved, confidence)`` or None to skip this commit.

    Posts the reading to the reference-verifier's ``/v1/residuals`` in ratio
    mode. ``--unsafe-approve-all`` short-circuits to a fixed verdict for
    testnet smoke only and is gated by ATTESTATION_LOOP_ALLOW_UNSAFE=1.
    """
    if cfg.unsafe_approve_all:
        return True, 0.75
    if not cfg.reference_url:
        raise SystemExit(
            "REFERENCE_URL unset: cannot decide attestations without the "
            "reference-verifier (/v1/residuals). --unsafe-approve-all is the only bypass."
        )
    observation = build_observation(cfg, item)
    if observation is None:
        return None
    status, body = _post_json(
        f"{cfg.reference_url}/v1/residuals",
        token="",
        body={"observations": [observation], "mode": "ratio"},
    )
    if status != 200:
        raise RuntimeError(f"reference-verifier /v1/residuals returned {status}: {body[:200]}")
    result = json.loads(body)["results"][0]
    decision = decide_from_result(result)
    if decision is None:
        print(
            f"[skip] {item['commit_id']}: reference-verifier returned no band ratio "
            "(no reference source could score this reading)"
        )
    return decision


# -------------------------------------------------------------- sign + submit


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
    pending_count = st.get("commits_pending_quorum", 0)
    print(
        f"[status] oracle_set={len(st.get('oracle_set', []))} quorum={st.get('oracle_quorum')} "
        f"pending={pending_count} with_quorum={st.get('commits_with_quorum')}"
    )
    items, skips = discover_pending(cfg, manual)
    for commit_id, reason in skips:
        print(f"[skip] {commit_id}: {reason}")
    submitted = 0
    for item in items:
        commit_id = item["commit_id"]
        decision = reference_check(cfg, item)
        if decision is None:
            continue
        approved, confidence = decision
        body = sign_attestation(cfg, commit_id, approved, confidence, item["nonce"])
        if not cfg.submit:
            print(
                f"[dry-run] would attest {commit_id}: approved={approved} confidence={confidence} "
                "(pass --submit to post; see shadow-mode note in the module docstring)"
            )
            continue
        status, resp = _post_json(
            f"{cfg.node_url}/transactions/oracle/attestation", cfg.oracle_token, body
        )
        ok = 200 <= status < 300
        submitted += ok
        print(
            f"[attest] {commit_id} approved={approved} conf={confidence} -> {status} "
            f"{'OK' if ok else resp[:160]}"
        )
    return submitted


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Reference-checked oracle attestation loop")
    p.add_argument("--once", action="store_true", help="run one cycle and exit")
    p.add_argument("--interval", type=float, default=60.0, help="seconds between cycles")
    p.add_argument(
        "--submit",
        action="store_true",
        help="POST signed attestations to the node. Default is dry-run: discover, "
        "reference-check and sign, but print instead of submitting (shadow mode).",
    )
    p.add_argument(
        "--manual-pending",
        type=str,
        metavar="FILE",
        help="LOCAL TESTING ONLY: JSON list of [{commit_id, reading, nonce, station_id?, "
        "timestamp?}] used instead of GET /transactions/oracle/pending",
    )
    p.add_argument(
        "--unsafe-approve-all",
        action="store_true",
        help="TESTNET SMOKE ONLY: skip the reference check and approve everything at a "
        "fixed confidence. Also requires ATTESTATION_LOOP_ALLOW_UNSAFE=1.",
    )
    return p


def main() -> None:
    args = build_parser().parse_args()
    cfg = Config.from_env(args)
    if not cfg.oracle_privkey or not cfg.oracle_token:
        raise SystemExit("ORACLE_PRIVKEY and ORACLE_TOKEN must be set")
    if not cfg.oracle_id:
        print("[warn] ORACLE_ID unset: self-skip disabled; a restart may re-attest until the node's "
              "attesting_oracles catches it")
    manual = None
    if args.manual_pending:
        with open(args.manual_pending, encoding="utf-8") as fh:
            manual = json.load(fh)

    if args.once:
        run_once(cfg, manual)
        return
    while True:
        try:
            run_once(cfg, manual)
        except Exception as e:  # noqa: BLE001 - outer loop reports failures and retries next cycle
            print(f"[error] cycle failed: {e}")
        time.sleep(cfg.interval_s)


if __name__ == "__main__":
    main()
