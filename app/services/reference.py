"""Batch reference computation: fetch tiles → interpolate → correct → package.

Per-source failures degrade to `error` strings on that source's entry; a
request never fails because one upstream is down.
"""

from __future__ import annotations

import math
import time as time_mod

from app import hypso, units
from app.bands import variables_for
from app.config import settings
from app.interp import bilinear, normalize_lon
from app.models import GridMeta, PointQuery, PointResult, SourceValues
from app.sources import registry
from app.sources.base import GridSample, SourceFetchError


def compute_reference(
    points: list[PointQuery],
    variables: list[str] | None,
    source_names: list[str] | None,
) -> list[PointResult]:
    requested_vars = variables or sorted(
        {"temperature_c", "pressure_hpa", "humidity_pct", "wind_speed_ms", "rainfall_mm"}
    )
    if source_names is None:
        source_names = [
            name
            for name in settings.enabled_sources
            if variables_for(name) & set(requested_vars)
        ]
    return [
        PointResult(point=p, sources=[
            _one_source(name, p, requested_vars) for name in source_names
        ])
        for p in points
    ]


def _one_source(name: str, point: PointQuery, requested_vars: list[str]) -> SourceValues:
    src = registry.source(name)
    if src is None:
        return SourceValues(source=name, error="source not enabled or no adapter attached")
    lon = normalize_lon(point.lon)
    # Serve exactly: requested ∩ adapter capability ∩ band table for the source.
    serve = [v for v in requested_vars if v in src.variables and v in variables_for(name)]
    if not serve:
        return SourceValues(source=name, error="source serves none of the requested variables")

    values: dict[str, float] = {}
    leads: dict[str, float] = {}
    correction: dict[str, bool] = {}
    grid: GridMeta | None = None
    datum: float | None = None
    cycle = None
    errors: list[str] = []

    for var in serve:
        started = time_mod.monotonic()
        try:
            sample = src.fetch(point.time, var, point.lat, lon)
            value, meta = interpolate_sample(sample, point.lat, lon)
            if var == "pressure_hpa":
                value, applied = _correct_pressure(src, sample, point, lon, value)
                correction["pressure_hpa"] = applied
                datum = meta.grid_elevation_m  # pressure datum (0.0 for MSLP)
            if not math.isfinite(value):
                raise SourceFetchError(f"{name}/{var}: reference value is not finite at point")
        except SourceFetchError as exc:
            errors.append(str(exc))
            registry.record_error(name, str(exc))
            continue
        registry.record_fetch(name, sample.cycle, time_mod.monotonic() - started)
        values[var] = round(value, 4)
        leads[var] = sample.lead_hours
        cycle = sample.cycle
        grid = meta

    if grid is not None:
        grid.elevation_correction_applied = correction
        grid.grid_elevation_m = datum

    if not values:
        return SourceValues(source=name, error="; ".join(errors) or "no data")
    return SourceValues(
        source=name,
        cycle=cycle,
        lead_hours=max(leads.values()),
        leads=leads,
        values=values,
        grid=grid,
        error="; ".join(errors) if errors else None,
    )


def interpolate_sample(sample: GridSample, lat: float, lon: float) -> tuple[float, GridMeta]:
    """Bilinear value + grid metadata; wind interpolates u and v separately."""
    if sample.components is not None:
        u = bilinear(sample.lats, sample.lons, sample.components["u"], lat, lon)
        v = bilinear(sample.lats, sample.lons, sample.components["v"], lat, lon)
        value, nearest = units.wind_speed(u.value, v.value), u.nearest_distance_km
    else:
        r = bilinear(sample.lats, sample.lons, sample.values, lat, lon)
        value, nearest = r.value, r.nearest_distance_km
    datum = None
    if sample.grid_elevation_m is not None:
        datum = bilinear(sample.lats, sample.lons, sample.grid_elevation_m, lat, lon).value
    meta = GridMeta(
        nearest_distance_km=round(nearest, 3),
        grid_elevation_m=datum,
        resolution_deg=sample.resolution_deg,
    )
    return value, meta


def _correct_pressure(
    src, pressure_sample: GridSample, point: PointQuery, lon: float, mslp_hpa: float
) -> tuple[float, bool]:
    """MSLP → station-elevation pressure via the hypsometric relation.

    Needs the station elevation and the source's 2 m temperature (fetched
    internally; on failure the uncorrected MSLP is returned, flagged)."""
    if point.elevation_m is None:
        return mslp_hpa, False
    try:
        t_sample = src.fetch(point.time, "temperature_c", point.lat, lon)
        t_c, _ = interpolate_sample(t_sample, point.lat, lon)
    except SourceFetchError:
        return mslp_hpa, False
    # Sea-level temperature of the column: station temp lapsed down.
    t_sl_k = units.c_to_k(t_c) + hypso.LAPSE * point.elevation_m
    corrected = hypso.correct_pressure_to_station(
        mslp_hpa, 0.0, point.elevation_m, t_sl_k
    )
    return corrected, True
