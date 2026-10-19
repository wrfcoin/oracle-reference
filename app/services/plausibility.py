"""Location plausibility: does a station's ≥7-day series physically match its
claimed coordinates?

Two independent checks:
- pressure-implied elevation (strongest GPS-spoof signal): invert the
  barometric formula per sample against the reference MSLP at the claimed
  point (standard atmosphere when the reference is unavailable), take the
  median, compare to the claimed elevation;
- temperature correlation grid-search: de-seasonalized station temperature
  anomalies correlated against every node of a reference t2m box around the
  claimed point (one vectorized matmul); the best-matching node localizes the
  station to ~one grid cell (10-25 km) — never finer.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import numpy as np
from fastapi import HTTPException

from app import hypso, units
from app.interp import haversine_km, normalize_lon
from app.models import (
    CorrelationCheck,
    ElevationCheck,
    PlausibilityRequest,
    PlausibilityResponse,
    SeriesInfo,
)

MIN_SPAN_DAYS = 7
MIN_SAMPLES = 40
# Hard rule: a pressure-implied elevation this far from the claim is
# physically irreconcilable regardless of the correlation score.
ELEVATION_HARD_LIMIT_M = 500.0
ELEVATION_CONSISTENT_M = 250.0
CORR_FLOOR = 0.2


class BoxSeries(NamedTuple):
    times: list[datetime]
    lats: np.ndarray  # 1-D ascending
    lons: np.ndarray  # 1-D ascending, [-180, 180)
    t2m_c: np.ndarray  # (time, ny, nx), degC
    mslp_hpa: np.ndarray  # (time, ny, nx), hPa


class PlausibilityDataError(RuntimeError):
    """Reference box series unavailable for the requested window/region."""


def _live_box_provider(
    source: str,
    times: list[datetime],
    lat0: float,
    lon0: float,
    radius_deg: float,
    step_deg: float,
) -> BoxSeries:
    """Fetch a t2m+MSLP box from the analysis source (era5/gfs). Heavy imports
    stay lazy; failures surface as PlausibilityDataError."""
    try:
        from earth2studio import data as e2d
    except ImportError as exc:  # pragma: no cover - slim CI never calls this
        raise PlausibilityDataError(f"earth2studio unavailable: {exc}") from exc
    lats = np.arange(lat0 - radius_deg, lat0 + radius_deg + 1e-9, step_deg)
    lons = np.arange(lon0 - radius_deg, lon0 + radius_deg + 1e-9, step_deg)
    try:
        cls = {"era5": e2d.ARCO, "gfs": e2d.GFS}[source]
        ds = cls(cache=True, verbose=False)
        da = ds(time=[t.replace(tzinfo=None) for t in times], variable=["t2m", "msl"])
        da = da.sel(lat=lats, lon=(lons % 360.0), method="nearest")
        t2m = np.asarray(da.sel(variable="t2m").values, dtype=float) - units.K_OFFSET
        msl = np.asarray(da.sel(variable="msl").values, dtype=float) / 100.0
    except Exception as exc:
        raise PlausibilityDataError(f"{source} box fetch failed: {exc}") from exc
    return BoxSeries(times=times, lats=lats, lons=lons, t2m_c=t2m, mslp_hpa=msl)


# Overridable provider (tests inject a recorded-fixture provider).
box_provider: Callable[..., BoxSeries] = _live_box_provider


def check_plausibility(req: PlausibilityRequest) -> PlausibilityResponse:
    opts = req.options
    lat0 = req.claimed.lat
    lon0 = normalize_lon(req.claimed.lon)

    times, temp, pres, info = _resample(req)

    box: BoxSeries | None = None
    try:
        box = box_provider(
            opts.reference_source, times, lat0, lon0, opts.search_radius_deg, opts.search_step_deg
        )
    except PlausibilityDataError:
        box = None  # both checks degrade (standard-atmosphere p0; no correlation)

    elevation = _elevation_check(req, times, temp, pres, box, lat0, lon0)
    correlation = _correlation_check(temp, box, lat0, lon0, opts.search_step_deg)

    scores = [c.score for c in (elevation, correlation) if c.available and c.score is not None]
    plausibility = round(float(np.mean(scores)), 3) if scores else 0.5

    verdict = "suspect"
    hard_elevation_fail = (
        elevation.available
        and elevation.delta_m is not None
        and abs(elevation.delta_m) > ELEVATION_HARD_LIMIT_M
    )
    if hard_elevation_fail or plausibility < 0.3:
        verdict = "inconsistent"
    elif plausibility >= 0.7:
        elevation_ok = (
            not elevation.available
            or elevation.delta_m is None
            or abs(elevation.delta_m) <= ELEVATION_CONSISTENT_M
        )
        match_ok = (
            not correlation.available
            or correlation.best_match_distance_km is None
            or correlation.best_match_distance_km
            <= 2.0 * opts.search_step_deg * 111.2
        )
        if elevation_ok and match_ok:
            verdict = "consistent"

    return PlausibilityResponse(
        verdict=verdict,
        plausibility=plausibility,
        checks={"pressure_elevation": elevation, "temperature_correlation": correlation},
        series=info,
        reference_source=opts.reference_source,
    )


def _resample(req: PlausibilityRequest):
    """Bucket the raw series onto the resample_hours grid (means per bucket).
    422 when the span or usable-sample count is too small to be meaningful."""
    buckets: dict[datetime, list[dict[str, float]]] = {}
    for s in req.series:
        t = s.time.astimezone(UTC)
        floored = t.replace(minute=0, second=0, microsecond=0) - timedelta(
            hours=t.hour % req.options.resample_hours
        )
        buckets.setdefault(floored, []).append(s.observation)

    times = sorted(buckets)
    # Covered window includes the final bucket's width.
    span = (
        times[-1] - times[0] + timedelta(hours=req.options.resample_hours)
        if len(times) > 1
        else timedelta(0)
    )
    if span < timedelta(days=MIN_SPAN_DAYS):
        raise HTTPException(
            status_code=422,
            detail=f"series too short: plausibility requires >= {MIN_SPAN_DAYS} days "
            f"(got {span.total_seconds() / 86400:.1f})",
        )
    if len(times) < MIN_SAMPLES:
        raise HTTPException(
            status_code=422,
            detail=f"series too sparse: >= {MIN_SAMPLES} resampled samples required "
            f"(got {len(times)})",
        )

    def bucket_mean(t: datetime, var: str) -> float:
        vals = [o[var] for o in buckets[t] if var in o]
        return float(np.mean(vals)) if vals else np.nan

    temp = np.array([bucket_mean(t, "temperature_c") for t in times])
    pres = np.array([bucket_mean(t, "pressure_hpa") for t in times])
    if not np.isfinite(temp).any():
        raise HTTPException(status_code=422, detail="series must include temperature_c")
    used = [v for v, arr in (("temperature_c", temp), ("pressure_hpa", pres)) if np.isfinite(arr).any()]
    info = SeriesInfo(
        n_samples_in=len(req.series),
        n_samples_used=len(times),
        start=times[0],
        end=times[-1],
        variables_used=used,
    )
    return times, temp, pres, info


def _elevation_check(
    req: PlausibilityRequest,
    times: list[datetime],
    temp: np.ndarray,
    pres: np.ndarray,
    box: BoxSeries | None,
    lat0: float,
    lon0: float,
) -> ElevationCheck:
    finite = np.isfinite(pres)
    if not finite.any():
        return ElevationCheck(available=False, claimed_elevation_m=req.claimed.elevation_m)
    # Reference MSLP at the claimed point per timestep (removes synoptic
    # swings); standard atmosphere when the box is unavailable.
    p0 = np.full(len(times), hypso.P0_HPA)
    if box is not None:
        cy = int(np.argmin(np.abs(box.lats - lat0)))
        cx = int(np.argmin(np.abs(box.lons - lon0)))
        by_time = {t: i for i, t in enumerate(box.times)}
        for i, t in enumerate(times):
            j = by_time.get(t)
            if j is not None:
                p0[i] = box.mslp_hpa[j, cy, cx]
    implied = np.array(
        [
            hypso.pressure_implied_elevation_m(
                float(pres[i]),
                t_obs_k=units.c_to_k(float(temp[i])) if np.isfinite(temp[i]) else None,
                p0_hpa=float(p0[i]),
            )
            for i in range(len(times))
            if finite[i]
        ]
    )
    implied_m = float(np.median(implied))
    claimed = req.claimed.elevation_m
    delta = implied_m - claimed if claimed is not None else None
    score = (
        max(0.0, min(1.0, 1.0 - abs(delta) / ELEVATION_HARD_LIMIT_M)) if delta is not None else None
    )
    return ElevationCheck(
        implied_elevation_m=round(implied_m, 1),
        claimed_elevation_m=claimed,
        delta_m=round(delta, 1) if delta is not None else None,
        n_samples=int(finite.sum()),
        score=round(score, 3) if score is not None else None,
        available=delta is not None,
    )


def _correlation_check(
    temp: np.ndarray,
    box: BoxSeries | None,
    lat0: float,
    lon0: float,
    step_deg: float,
) -> CorrelationCheck:
    finite = np.isfinite(temp)
    if box is None or finite.sum() < MIN_SAMPLES // 2:
        return CorrelationCheck(available=False)
    if not (box.lats.min() <= lat0 <= box.lats.max() and box.lons.min() <= lon0 <= box.lons.max()):
        return CorrelationCheck(available=False)

    t2m = box.t2m_c[finite]  # (T, ny, nx)
    station = temp[finite]
    hours = np.array([t.hour for t, ok in zip(box.times, finite) if ok])

    # De-seasonalize: subtract each series' mean diurnal cycle so the
    # correlation discriminates weather, not climatology.
    station_a = _deseasonalize(station[:, None, None], hours)[:, 0, 0]
    grid_a = _deseasonalize(t2m, hours)

    T = station_a.shape[0]
    grid_flat = grid_a.reshape(T, -1).T  # (N, T)
    gc = grid_flat - grid_flat.mean(axis=1, keepdims=True)
    sc = station_a - station_a.mean()
    denom = np.linalg.norm(gc, axis=1) * np.linalg.norm(sc)
    with np.errstate(invalid="ignore", divide="ignore"):
        corr = np.where(denom > 0, (gc @ sc) / denom, 0.0)
    corr_grid = corr.reshape(grid_a.shape[1], grid_a.shape[2])

    best_idx = np.unravel_index(int(np.nanargmax(corr_grid)), corr_grid.shape)
    best_lat = float(box.lats[best_idx[0]])
    best_lon = float(box.lons[best_idx[1]])
    best_corr = float(corr_grid[best_idx])
    cy = int(np.argmin(np.abs(box.lats - lat0)))
    cx = int(np.argmin(np.abs(box.lons - lon0)))
    corr_at_claimed = float(corr_grid[cy, cx])

    if best_corr > CORR_FLOOR:
        score = max(0.0, min(1.0, (corr_at_claimed - CORR_FLOOR) / (best_corr - CORR_FLOOR)))
    else:
        score = 0.5  # nothing in the box correlates: uninformative
    return CorrelationCheck(
        corr_at_claimed=round(corr_at_claimed, 3),
        best_corr=round(best_corr, 3),
        best_match={"lat": best_lat, "lon": best_lon},
        best_match_distance_km=round(haversine_km(lat0, lon0, best_lat, best_lon), 1),
        grid_step_deg=step_deg,
        score=round(score, 3),
        available=True,
    )


def _deseasonalize(series: np.ndarray, hours: np.ndarray) -> np.ndarray:
    """Subtract the per-hour-of-day mean along axis 0."""
    out = series.astype(float).copy()
    for h in np.unique(hours):
        sel = hours == h
        out[sel] -= out[sel].mean(axis=0, keepdims=True)
    return out
