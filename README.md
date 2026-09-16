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

## Attestation loop (`agent/attestation_loop.py`)

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
bands_version}`.

- `residuals` — **observed − reference, canonical units, sign preserved** (core4
  `ReferenceResidual` semantics; the backend shadow scorer consumes this — never normalized).
- `band_ratios` — `|residual| / marginal_max` from the embedded bands v1 (byte-parity copy of
  `core4/shared-protocol/fixtures/reference_bands.json`; drift-alarm tested). Bands are public
  data, not verdicts.
- `mode` selects the space of the top-level thresholds, matching how the consumer compares:
  `"raw"` (default) scales thresholds per result so `max(|residuals|) <= marginal_threshold`
  decides exactly as the band-ratio comparison (the attestation loop works verbatim);
  `"ratio"` returns the stable dimensionless constants (marginal 1.0, outlier 2.0) for
  consumers reading `band_ratios`.

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
