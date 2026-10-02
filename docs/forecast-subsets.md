# Forecast point extraction

`POST /v1/forecast` extracts GFS_FX or GEFS_FX values from an operator-staged
normalized GRIB subset. It is a house-baseline data API, with no scoring or
reward policy. Model/source is explicit so additional models can extend the
same response contract later.

```json
{
  "source": "gefs_fx",
  "cycle": "2026-01-01T00:00:00Z",
  "points": [{"lat": 0, "lon": 1}],
  "lead_hours": [6, 12],
  "variables": ["temperature_c"],
  "members": ["control", "p01"]
}
```

Response fields are `source`, `cycle`, `service` and `results`. Each result
contains its `point` and a `forecasts` lead matrix; each lead has `lead_hours`,
`valid_time` and `members`, each with `member` and canonical `values`.
Requested point, lead and member order is preserved. Omitted members select
all members available in the subset; deterministic GFS has only `control`.
Cycle timestamps must be aware six-hour UTC cycles. The service never silently
substitutes a newer cycle, lead or ensemble member. Out-of-tile points are
refused, and both signed and 0–360 longitudes are accepted.

## Normalized subset schema v1

The checked-in `tests/fixtures/forecast_subset_v1.json` documents a small
synthetic 2×2 grid with two leads and three GEFS members. Its center values
are the arithmetic means of four corners: 13/14/12 at lead 6 and 23/24/22 at
lead 12. It contains no retrieved weather or station data.

Fields are `version: 1`, `source`, `cycle`, ascending 1-D `lats`/`lons`,
`lead_hours`, `members`, and `variables`. Each variable is a finite array
shaped `[lead][member][latitude][longitude]`. Longitudes in subsets are in
[-180,180]. Values use temperature °C, pressure hPa, humidity %, wind speed
m/s and rainfall mm. Pressure is model mean-sea-level pressure; no station
elevation correction is applied by this forecast endpoint. Rainfall values
retain the accumulation interval used by the producing subset feed; consumers
must compare an equivalent interval. Wind here is an already normalized
scalar, so bilinear scalar interpolation applies.

An operator's GRIB extraction step must preserve the requested model cycle,
valid lead and member identity, convert units and arrange these axes. This PR
implements extraction from that documented format; it does not include a
GRIB-to-JSON converter or bulk GFS/GEFS downloader. GFS and GEFS datasets may
use different grids; each subset declares its own axes.

## Configuration and bounds

Set `FORECAST_SUBSET_DIR` to the staged archive directory. Files are named
`<source>/<YYYYMMDDHH>.json`, for example `gefs_fx/2026010100.json`.
Without an archive the endpoint returns 503. Missing cycle files also return
503. Invalid archive data returns a sanitized 502; unsupported requested
axes or points return 422.

Remote subset retrieval is OFF by default. It requires both
`FORECAST_LIVE_FETCH=1` and a trusted operator-configured HTTPS
`FORECAST_SUBSET_URL`. This URL serves the same pre-extracted schema/paths,
not NOAA's whole GRIB archives. Local archives take precedence. Request
payloads cannot supply a URL or filesystem path. Retrieval uses
`FETCH_TIMEOUT_S` and reads at most 1 MiB plus one sentinel byte; oversized
local/remote subsets are refused before JSON parsing. Decoded fields are
limited to 100,000 cells; extraction is limited to 10,000 point-values,
100 points, 64 leads, 31 members and 5 variables. No remote transport is
used by tests: a tiny fake byte stream exercises the opt-in and byte limit.

Operational preparation consists of staging normalized subsets and selecting
archive configuration before rebuilding the service image. No consensus
feature, migration or activation schedule is changed. Backend duel integration
must select the required source, cycle and leads explicitly; unavailable data
must remain unavailable instead of becoming a fabricated baseline.
