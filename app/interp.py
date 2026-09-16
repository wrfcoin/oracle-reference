"""Bilinear interpolation of a grid tile to a station point. Pure numpy.

Two grid shapes are supported:
- regular lat/lon grids (GFS, ERA5, MRMS): 1-D ascending ``lats``/``lons``;
- curvilinear grids (HRRR's Lambert-conformal projection): 2-D ``lats``/``lons``
  coordinate arrays, interpolated with an exact inverse-bilinear quad mapping.

Longitudes must already be normalized to [-180, 180) by the adapter
(HRRR/GFS/ERA5 natively use 0–360).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

EARTH_RADIUS_KM = 6371.0


def normalize_lon(lon: float) -> float:
    """Any longitude → [-180, 180)."""
    return (lon + 180.0) % 360.0 - 180.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


@dataclass(frozen=True)
class InterpResult:
    value: float
    nearest_distance_km: float


def bilinear(
    lats: np.ndarray,
    lons: np.ndarray,
    values: np.ndarray,
    lat: float,
    lon: float,
) -> InterpResult:
    """Interpolate ``values`` to (lat, lon). Dispatches on coordinate rank."""
    lats = np.asarray(lats, dtype=float)
    lons = np.asarray(lons, dtype=float)
    values = np.asarray(values, dtype=float)
    if lats.ndim == 1:
        return _bilinear_regular(lats, lons, values, lat, lon)
    return _bilinear_curvilinear(lats, lons, values, lat, lon)


def _bilinear_regular(
    lats: np.ndarray, lons: np.ndarray, values: np.ndarray, lat: float, lon: float
) -> InterpResult:
    if lats[0] > lats[-1] or lons[0] > lons[-1]:
        raise ValueError("regular grid coordinates must be ascending")
    j = int(np.clip(np.searchsorted(lats, lat) - 1, 0, len(lats) - 2))
    i = int(np.clip(np.searchsorted(lons, lon) - 1, 0, len(lons) - 2))
    # Fractional position within the cell, clamped so points on/just past the
    # tile edge use the edge value instead of extrapolating.
    t = float(np.clip((lat - lats[j]) / (lats[j + 1] - lats[j]), 0.0, 1.0))
    s = float(np.clip((lon - lons[i]) / (lons[i + 1] - lons[i]), 0.0, 1.0))
    v = (
        values[j, i] * (1 - t) * (1 - s)
        + values[j, i + 1] * (1 - t) * s
        + values[j + 1, i] * t * (1 - s)
        + values[j + 1, i + 1] * t * s
    )
    jn = j if t < 0.5 else j + 1
    in_ = i if s < 0.5 else i + 1
    dist = haversine_km(lat, lon, float(lats[jn]), float(lons[in_]))
    return InterpResult(value=float(v), nearest_distance_km=dist)


def _bilinear_curvilinear(
    lats: np.ndarray, lons: np.ndarray, values: np.ndarray, lat: float, lon: float
) -> InterpResult:
    if lats.shape != lons.shape or lats.shape != values.shape:
        raise ValueError("curvilinear grid arrays must share one 2-D shape")
    coslat = math.cos(math.radians(lat))
    d2 = (lats - lat) ** 2 + (coslat * (lons - lon)) ** 2
    jn, in_ = np.unravel_index(int(np.argmin(d2)), d2.shape)
    dist = haversine_km(lat, lon, float(lats[jn, in_]), float(lons[jn, in_]))

    nj, ni = lats.shape
    best: tuple[float, float, int, int] | None = None  # (s, t, j0, i0)
    for j0 in (jn - 1, jn):
        for i0 in (in_ - 1, in_):
            if not (0 <= j0 <= nj - 2 and 0 <= i0 <= ni - 2):
                continue
            st = _inverse_bilinear(
                lats[j0 : j0 + 2, i0 : i0 + 2], lons[j0 : j0 + 2, i0 : i0 + 2], lat, lon
            )
            if st is None:
                continue
            s, t = st
            eps = 1e-6
            if -eps <= s <= 1 + eps and -eps <= t <= 1 + eps:
                best = (min(max(s, 0.0), 1.0), min(max(t, 0.0), 1.0), j0, i0)
                break
        if best:
            break
    if best is None:
        # Point outside the tile's quad hull (edge of tile): nearest node.
        return InterpResult(value=float(values[jn, in_]), nearest_distance_km=dist)
    s, t, j0, i0 = best
    q = values[j0 : j0 + 2, i0 : i0 + 2]
    v = (
        q[0, 0] * (1 - t) * (1 - s)
        + q[0, 1] * (1 - t) * s
        + q[1, 0] * t * (1 - s)
        + q[1, 1] * t * s
    )
    return InterpResult(value=float(v), nearest_distance_km=dist)


def _inverse_bilinear(
    qlat: np.ndarray, qlon: np.ndarray, lat: float, lon: float
) -> tuple[float, float] | None:
    """Invert the bilinear map of a 2×2 quad: (s, t) such that the bilinear
    combination of the quad corners equals (lat, lon). Newton iteration —
    converges in a few steps for the near-parallelogram cells of model grids.
    """
    s, t = 0.5, 0.5
    for _ in range(10):
        f_lat = (
            qlat[0, 0] * (1 - t) * (1 - s)
            + qlat[0, 1] * (1 - t) * s
            + qlat[1, 0] * t * (1 - s)
            + qlat[1, 1] * t * s
            - lat
        )
        f_lon = (
            qlon[0, 0] * (1 - t) * (1 - s)
            + qlon[0, 1] * (1 - t) * s
            + qlon[1, 0] * t * (1 - s)
            + qlon[1, 1] * t * s
            - lon
        )
        if abs(f_lat) < 1e-10 and abs(f_lon) < 1e-10:
            break
        d_lat_ds = (qlat[0, 1] - qlat[0, 0]) * (1 - t) + (qlat[1, 1] - qlat[1, 0]) * t
        d_lat_dt = (qlat[1, 0] - qlat[0, 0]) * (1 - s) + (qlat[1, 1] - qlat[0, 1]) * s
        d_lon_ds = (qlon[0, 1] - qlon[0, 0]) * (1 - t) + (qlon[1, 1] - qlon[1, 0]) * t
        d_lon_dt = (qlon[1, 0] - qlon[0, 0]) * (1 - s) + (qlon[1, 1] - qlon[0, 1]) * s
        det = d_lat_ds * d_lon_dt - d_lat_dt * d_lon_ds
        if abs(det) < 1e-14:
            return None
        s -= (f_lat * d_lon_dt - f_lon * d_lat_dt) / det
        t -= (f_lon * d_lat_ds - f_lat * d_lon_ds) / det
    return s, t
