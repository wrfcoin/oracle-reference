# Attestation loop (historical, retired)

> **Historical. Do not run.** `agent/attestation_loop.py` was retired on 2026-10-03 and superseded
> by the core4 attester
> ([`native-chain/src/bin/oracle_attester.rs`](https://github.com/wrfcoin/core4/blob/main/native-chain/src/bin/oracle_attester.rs)).
> These notes describe how the loop worked before retirement. They are not instructions: do not
> package it or run it against any node, and in particular never use `--submit`.

One oracle operator runs this against a node and this service. Per cycle it discovers pending
commits from the node's real `GET /transactions/oracle/pending` (core4#2464), skips anything at
quorum or already attested by `ORACLE_ID`, reference-checks each reading via `POST /v1/residuals`
(`mode: "ratio"`: approve iff `worst_ratio <= 1.0`, `confidence = 1 - worst_ratio / 2`), signs
with the Rust `oracle_attest_sign` tool, and — only with `--submit` — posts to
`/transactions/oracle/attestation`.

```bash
NODE_URL=http://localhost:8556 ORACLE_TOKEN=… ORACLE_PRIVKEY=… ORACLE_ID=… \
REFERENCE_URL=http://localhost:8010 STATION_LOCATIONS_FILE=stations.json \
python -m agent.attestation_loop --once            # dry-run: discover, check, sign, print
python -m agent.attestation_loop --submit          # actually attest (see below)
```

**Shadow mode still holds.** This service's scores stay advisory. A *submitted* attestation,
however, counts toward the node's oracle quorum and therefore contributor rewards — so the loop's
default is dry-run and submission needs the explicit `--submit` flag. Wiring the automation
(infra#945 / #8) is not the Phase 0.5 enforcement flip; running it with `--submit` on a
reward-bearing network is that human decision.

The node's pending endpoint does not carry station coordinates, which `/v1/residuals` needs:
supply `STATION_LOCATIONS_FILE` (`{station_id: {lat, lon, elevation_m?}}`; backend's
`GET /api/weather/stations` rows reduce to this shape) or embed `lat`/`lon` in the reading. A
commit with no known location is skipped with a printed reason, never approved blind. Escape
hatches are explicit and never defaults: `--unsafe-approve-all` (also needs
`ATTESTATION_LOOP_ALLOW_UNSAFE=1`) and `--manual-pending FILE`.
