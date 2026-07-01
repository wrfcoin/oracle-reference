# oracle-reference

Independent weather reference-verification service for the WRFCoin oracle
(core4 issue [#724](https://github.com/wrfcoin/core4/issues/724) — oracle trust bootstrap).

Wraps [NVIDIA earth2studio](https://github.com/NVIDIA/earth2studio) **data sources only**
(GFS/GFS_FX, HRRR/HRRR_FX, MRMS, ERA5, ISD/GHCND) to serve gridded reference values,
observation residuals, and station location-plausibility checks over REST.
CPU-only by construction — no model inference, no GPU anywhere in the build.

## Design invariants

- **No tier verdicts here.** This service returns reference values and residuals only.
  The consensus-adjacent residual → quality-tier mapping lives in Rust
  (`core4/shared-protocol/src/weather/reference.rs`) so any node can independently
  re-derive tiers from public data.
- **Two-pass scoring model** (consumed by the backend):
  advisory at reveal vs short-lead HRRR_FX/GFS_FX forecasts (no same-instant analysis
  exists yet), authoritative at T+6h vs actual HRRR/GFS analysis.
- **Shadow mode first**: advisory scores and metrics only; zero reward impact until the
  Phase 0.5 enforcement flip is human-approved.

## API (Phase 0)

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | liveness + cached source cycles |
| `GET /v1/sources/status` | per-source latest cycle, fetch latency, error counts |
| `POST /v1/reference` | batch point/time → per-source reference values (SI + project units, elevation-corrected pressure) |
| `POST /v1/residuals` | observations in → per-source residuals out |
| `POST /v1/location-plausibility` | station series vs claimed coords → plausibility, pressure-implied elevation, best-match location |

Runs on port 8010 inside the `wrfcoin-testnet` bridge network (no host binding).
Deployment: local builds only via the infra testnet compose stack — never pushed to a registry.
