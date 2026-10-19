# oracle-reference

A CPU-only weather reference service for comparing observations with upstream weather datasets.

The reference API and the retired Python attestation loop have different roles: do not deploy the retired loop. This repository is not a forecast-training or payout-validation service.

## Public testnet

Test status (2026-10-19): `pytest -q` passed at `2f49bff` on Linux/Python 3.12 (182 passed, 2 skipped, 1 deselected; recorded-fixture suite, not live-upstream validation).

Public testnet launch: **2026-10-22**. Testnet tokens have no monetary value, and the network may be reset.

- [WRFCoin website](https://wrfcoin.com)
- [Public testnet quickstart](https://github.com/wrfcoin/docs/blob/main/testnet/quickstart.md)
- [Help and bug reports](https://github.com/wrfcoin/testnet-support)
- [Security reporting](SECURITY.md)

## Today and planned work

| Status | Scope |
| --- | --- |
| Available in this repository today | Reference values, observation residuals, location-plausibility checks and bounded operator-staged forecast point extraction. |
| Planned / not available here | Any change to reward enforcement or oracle deployment requires separate review; local API tests do not establish live integration or payout readiness. |

## Build and use

See [CONTRIBUTING.md](CONTRIBUTING.md#build-and-test) for prerequisites and build/test commands. CI results apply only to the named commit and executed checks; they do not prove deployment.

Licensed under [MIT](LICENSE-MIT) or [Apache-2.0](LICENSE-APACHE), at your option.

Forecast point extraction is available at `POST /v1/forecast` using bounded
operator-staged GFS/GEFS subsets. See [the subset contract](docs/forecast-subsets.md)
for request/response fields, canonical units, limits and optional retrieval.

Independent weather reference-verification service for the WRFCoin oracle
(core4 issue #724 (internal historical reference) — oracle trust bootstrap).

Wraps [NVIDIA earth2studio](https://github.com/NVIDIA/earth2studio) **data sources only**
(GFS/GFS_FX, HRRR/HRRR_FX, MRMS, ERA5, ISD/GHCND) to serve gridded reference values,
observation residuals, and station location-plausibility checks over REST.
CPU-only by construction — no model inference, no GPU anywhere in the build.

## Design invariants

- **Advisory tier metadata mirrors deployed consensus.** Reference values and residuals
  remain authoritative inputs; the additive tier mirrors core4 `9ef92a235`
  (`shared-protocol/src/weather/reference.rs`). It does not change the agent's
  confidence calculation or reward enforcement.
- **Two-pass scoring model** (consumed by the backend):
  advisory at reveal vs short-lead HRRR_FX/GFS_FX forecasts (no same-instant analysis
  exists yet), authoritative at T+6h vs actual HRRR/GFS analysis.
- **Shadow mode first**: advisory scores and metrics only; zero reward impact until the
  Phase 0.5 enforcement flip is human-approved.

## Attestation loop (`agent/attestation_loop.py`)

> **RETIRED: superseded by the core4 attester; do not deploy.** Owner ruling 2026-10-03.
> The core4 attester performs reference-checked attestation, so this Python loop is kept for
> history only and must not be packaged or run against a live node. The reference service
> itself (the `/v1/*` endpoints used by shadow scoring) is unaffected.

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

## API (Phase 0)

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | liveness + cached source cycles |
| `GET /v1/sources/status` | per-source latest cycle, fetch latency, error counts, cache usage |
| `POST /v1/reference` | batch point/time → per-source reference values (canonical units, elevation-corrected pressure) |
| `POST /v1/residuals` | observations in → per-source residuals out |
| `POST /v1/location-plausibility` | ≥7-day station series vs claimed coords → plausibility, pressure-implied elevation, best-match location |

Canonical variables/units (matching core4 shared-protocol): `temperature_c` (°C),
`pressure_hpa` (hPa), `humidity_pct` (%), `wind_speed_ms` (m/s), `rainfall_mm` (mm).
Sources: `hrrr`, `hrrr_fx` (f01–f02 advisory), `gfs`, `gfs_fx`, `mrms` (rainfall only),
`era5` (no rainfall). Per-source upstream failures degrade to `error` strings on that
source's entry — a batch request never fails because one upstream is down.

### `POST /v1/reference`

Request: `{"points": [{"lat", "lon", "elevation_m", "time"}], "variables"?: [...], "sources"?: [...]}`
(≤100 points; timestamps must be timezone-aware). Response per point × source:
`{source, cycle, lead_hours, leads: {var: h}, values: {var: float}, grid: {nearest_distance_km,
grid_elevation_m, resolution_deg, elevation_correction_applied}, error}`.

Pressure note: HRRR/GFS expose no usable model orography, so `pressure_hpa` references are
model **MSLP corrected hypsometrically to the station elevation** (using the source's own
2 m temperature). This removes grid-vs-station elevation error — the largest false-residual
source — entirely; `grid_elevation_m` reports the pre-correction datum (0.0). Without
`elevation_m` the raw MSLP is returned and flagged `elevation_correction_applied: false`.

`lead_hours` is 0 for true analyses. GFS cycles are 6-hourly, so off-cycle valid times are
served from the latest cycle's short-lead forecast and report their real lead. Rainfall is
always the 1-h accumulation ending at the requested time (HRRR f01 of the prior cycle;
GFS APCP bucket differencing).

### `POST /v1/residuals`

Request: `{"observations": [{lat, lon, elevation_m, time | observed_at_unix, observation:
{var: value}}], "sources"?: [...], "mode"?: "raw" | "ratio"}`. Flattened core4 canonical
readings (variables at the top level) are also accepted.

Response per observation: `{per_source: [{source, cycle, lead_hours, residuals, band_ratios,
reference, grid, error}], worst_ratio, worst, marginal_threshold, outlier_threshold,
bands_version, tier, fold_rule, gross_guard_active}`.

- `residuals` — **observed − reference, canonical units, sign preserved** (core4
  `ReferenceResidual` semantics; the backend shadow scorer consumes this — never normalized).
- `band_ratios` — `|residual| / marginal_max` from the embedded bands v3 (byte-parity copy of
  `core4/shared-protocol/fixtures/reference_bands.json`; drift-alarm tested). Bands are public
  data, not verdicts.
- `mode` selects the space of the top-level thresholds, matching how the consumer compares:
  `"raw"` (default) scales thresholds per result so `max(|residuals|) <= marginal_threshold`
  decides exactly as the band-ratio comparison (the attestation loop works verbatim);
  `"ratio"` returns the stable dimensionless constants (marginal 1.0, outlier 2.0) for
  consumers reading `band_ratios`.

Optional `tiering_context: {"height": 100, "gross_guard_activation_height": 100}`
selects the guard using the chain feature schedule, inclusively at activation height.
The caller supplies the correct chain context; this service never guesses from time
or fetches a live node. Omitted context preserves the pre-activation second-worst
rule; a null/omitted activation height means unscheduled. Every result records the
chosen `gross_guard_active` state and `fold_rule`. Heights must be u64 integers.

The guard uses the fixture's optional per-variable `gross_max`, strictly exceeded
by a finite residual. Exactly one outlier among at least two scored residuals then
collapses to outlier. Missing bands are skipped; nonfinite residuals tier outlier
but are not gross. The old v2/multiplier issue wording predates the deployed v3
fixture. Canonical fixtures and pre/post golden vectors are copied byte-for-byte
from `9ef92a235`; no new threshold policy is introduced here. `nws_station` bands
are supported even if that source's adapter is unavailable in a deployment.

### `POST /v1/location-plausibility`

Request: `{"claimed": {lat, lon, elevation_m}, "series": [{time, observation}], "options"?:
{reference_source: "era5"|"gfs", search_radius_deg, search_step_deg, resample_hours}}`.
Requires ≥7 days of data and ≥40 resampled samples (422 otherwise); `temperature_c` required,
`pressure_hpa` strongly recommended (it carries the strongest GPS-spoof signal).

Two independent checks: **pressure-implied elevation** (barometric inversion per sample
against reference MSLP at the claimed point, median over the series; |Δ| > 500 m is
hard-`inconsistent`) and **temperature correlation** (de-seasonalized station anomalies vs
every node of a reference box; the best-matching node localizes the station). Verdict:
`consistent | suspect | inconsistent`. Limitation: resolution is ~one reference grid cell
(10–25 km) — verdicts localize region, not address.

## Testing

- `pytest -q` — full suite on recorded fixtures (no network, slim deps: `requirements-ci.txt`).
- `pytest -m grib -o addopts=""` — real GRIB2 decode of the checked-in MRMS sample
  (needs cfgrib/eccodes; auto-skips otherwise).
- `pytest -m live -o addopts="" tests/test_live.py` — manual/nightly: `/v1/reference` t2m at
  Duluth vs the same-hour KDLH METAR, within ~2 °C (proves interpolation/units/elevation chain).
- `python scripts/record_fixtures.py --era5-box --series --mrms-grib` — regenerate fixtures
  (network + full deps; pinned to 2024-06-24T12Z).

Runs on port 8010 inside the `wrfcoin-testnet` bridge network (no host binding).
Deployment: local builds only via the infra testnet compose stack — never pushed to a registry.
Cache janitor: background sweep every `JANITOR_INTERVAL_S` (default 900 s) enforces per-source
retention (HRRR 7 d, GFS 14 d, MRMS 48 h, ERA5 pinned) then LRU-evicts to `CACHE_MAX_GB`.

## License

Licensed under [MIT](LICENSE-MIT) or [Apache-2.0](LICENSE-APACHE), at your option.
