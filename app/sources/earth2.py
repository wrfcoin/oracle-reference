"""earth2studio-backed adapters: hrrr, hrrr_fx, gfs, gfs_fx, era5 (ARCO).

All earth2studio/xarray/torch imports happen inside methods so the app
imports (and the whole test suite runs) under slim CI dependencies.

Canonical-variable mapping notes (verified against earth2studio 0.16 lexicons):

- temperature_c  <- t2m (K)
- pressure_hpa   <- msl (Pa). Model MSLP, not surface pressure: HRRR/GFS
  lexicons expose no model orography, and comparing station pressure via
  MSLP + hypsometric correction to the station elevation (done in the
  service layer; the tile's ``grid_elevation_m`` is the 0 m datum) removes
  the grid-vs-station elevation error — the biggest false-residual source —
  without needing the model's terrain height at all.
- humidity_pct   <- r2m (HRRR/GFS, direct) or d2m+t2m via Magnus (ERA5).
- wind_speed_ms  <- u10m/v10m. The tile carries both components so the
  service can interpolate each and THEN take the magnitude.
- rainfall_mm    <- tp (APCP, kg m-2 == mm). Analysis files carry zero
  accumulation ("0-0 hour acc"), so the 1-h accumulation ending at T comes
  from the forecast variant: f01 of the T-1h cycle (HRRR) or a bucket
  difference (GFS restarts its APCP bucket every 6 h).

GFS caveat: GFS cycles are 6-hourly, so an off-cycle valid time is served
from the latest cycle's short-lead forecast — ``lead_hours`` in the response
reports this honestly (0 only at true analysis hours).
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta

import numpy as np

from app import units
from app.config import settings
from app.interp import normalize_lon
from app.sources.base import GridSample, SourceFetchError

_TILE_HALF = 3  # tile is (2*_TILE_HALF)x(2*_TILE_HALF) grid nodes

_CANONICAL = frozenset(
    {"temperature_c", "pressure_hpa", "humidity_pct", "wind_speed_ms", "rainfall_mm"}
)


def _utc_hour(t: datetime) -> datetime:
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


class Earth2Source:
    """One adapter instance per source name; behavior differences are data."""

    def __init__(self, name: str):
        if name not in {"hrrr", "hrrr_fx", "gfs", "gfs_fx", "era5"}:
            raise ValueError(f"unknown earth2studio source {name}")
        self.name = name
        self.variables = _CANONICAL - ({"rainfall_mm"} if name == "era5" else set())
        self.max_lead_hours = {"hrrr_fx": 2.0, "gfs_fx": 6.0}.get(name, 0.0)
        self._cycle_step_h = 6 if name.startswith("gfs") else 1
        self._base = "arco" if name == "era5" else name.removesuffix("_fx")
        self._lock = threading.Lock()
        self._clients: dict[str, object] = {}
        self._last_cycle: datetime | None = None

    def cache_subdir(self) -> str:
        # earth2studio writes one subdir per data class under
        # EARTH2STUDIO_CACHE (confirmed empirically: hrrr, gfs, arco).
        return self._base

    # -- earth2studio client management --------------------------------------

    def _client(self, kind: str):
        """kind: analysis | forecast. Instantiated lazily, once, per adapter."""
        with self._lock:
            if kind in self._clients:
                return self._clients[kind]
            os.environ.setdefault("EARTH2STUDIO_CACHE", settings.cache_dir)
            from earth2studio import data as e2d

            if kind == "analysis":
                cls = {"hrrr": e2d.HRRR, "gfs": e2d.GFS, "arco": e2d.ARCO}[self._base]
            else:
                cls = {"hrrr": e2d.HRRR_FX, "gfs": e2d.GFS_FX}[self._base]
            kwargs: dict = {"cache": True, "verbose": False}
            if self._base != "arco":
                kwargs["async_timeout"] = int(settings.fetch_timeout_s)
            self._clients[kind] = cls(**kwargs)
            return self._clients[kind]

    # -- ReferenceSource protocol ---------------------------------------------

    def fetch(self, time: datetime, variable: str, lat: float, lon: float) -> GridSample:
        if variable not in self.variables:
            raise SourceFetchError(f"{self.name} does not serve {variable}")
        valid = _utc_hour(time)
        try:
            if variable == "rainfall_mm":
                sample = self._fetch_rainfall(valid, lat, lon)
            else:
                sample = self._fetch_instant(variable, valid, lat, lon)
        except SourceFetchError:
            raise
        except Exception as exc:
            raise SourceFetchError(
                f"{self.name} fetch failed for {variable}@{valid.isoformat()}: {exc}"
            ) from exc
        self._last_cycle = sample.cycle
        return sample

    def latest_cycle(self) -> datetime | None:
        return self._last_cycle

    # -- fetch paths ------------------------------------------------------------

    def _cycle_and_lead(self, valid: datetime) -> tuple[datetime, float]:
        """Cycle/lead pair whose valid time is ``valid``."""
        step = self._cycle_step_h
        cycle = valid.replace(hour=(valid.hour // step) * step)
        if self.name == "hrrr_fx":
            # Reveal-time advisory: short-lead forecast (f01).
            return valid - timedelta(hours=1), 1.0
        if self.name == "gfs_fx":
            # Always a genuine forecast: step back one cycle at analysis hours.
            if cycle == valid:
                cycle -= timedelta(hours=step)
            return cycle, (valid - cycle).total_seconds() / 3600.0
        return cycle, (valid - cycle).total_seconds() / 3600.0

    def _fetch_instant(
        self, variable: str, valid: datetime, lat: float, lon: float
    ) -> GridSample:
        native = {
            "temperature_c": ["t2m"],
            "pressure_hpa": ["msl"],
            "humidity_pct": ["d2m", "t2m"] if self.name == "era5" else ["r2m"],
            "wind_speed_ms": ["u10m", "v10m"],
        }[variable]
        cycle, lead = self._cycle_and_lead(valid)
        tiles = self._fetch_tiles(native, cycle, [lead], lat, lon)
        components = None
        grid_elev = None
        if variable == "temperature_c":
            values = tiles["t2m"] - units.K_OFFSET
        elif variable == "pressure_hpa":
            values = tiles["msl"] / 100.0
            grid_elev = np.zeros_like(values)  # MSLP datum: sea level
        elif variable == "humidity_pct":
            if self.name == "era5":
                t_c = tiles["t2m"] - units.K_OFFSET
                td_c = tiles["d2m"] - units.K_OFFSET
                values = np.vectorize(units.rh_from_t_dewpoint)(t_c, td_c).astype(float)
            else:
                values = np.clip(tiles["r2m"], 0.0, 100.0)
        else:  # wind_speed_ms
            values = np.hypot(tiles["u10m"], tiles["v10m"])
            components = {"u": tiles["u10m"], "v": tiles["v10m"]}
        return GridSample(
            variable=variable,
            lats=tiles["lats"],
            lons=tiles["lons"],
            values=values,
            grid_elevation_m=grid_elev,
            cycle=cycle,
            lead_hours=lead,
            valid_time=valid,
            resolution_deg=tiles["resolution_deg"],
            components=components,
        )

    def _fetch_rainfall(self, valid: datetime, lat: float, lon: float) -> GridSample:
        """1-h accumulation ending at ``valid`` via the forecast variant."""
        if self._base == "hrrr":
            if self.name == "hrrr_fx":
                cycle, lead = self._cycle_and_lead(valid)
            else:
                cycle, lead = valid - timedelta(hours=1), 1.0
            leads = [lead]
        else:
            cycle, lead = self._cycle_and_lead(valid)
            if lead == 0.0:  # gfs analysis hour: use prior cycle's f06 bucket
                cycle -= timedelta(hours=self._cycle_step_h)
                lead = float(self._cycle_step_h)
            # GFS APCP bucket restarts every 6 h: the hourly amount ending at
            # lead n is tp(n) when (n-1) % 6 == 0, else tp(n) - tp(n-1).
            leads = [lead] if (lead - 1) % 6 == 0 else [lead - 1.0, lead]
        tiles = self._fetch_tiles(["tp"], cycle, leads, lat, lon)
        if len(leads) == 2:
            values = tiles["tp"] - tiles["tp_prev"]
        else:
            values = tiles["tp"]
        values = np.clip(values, 0.0, None)  # guard tiny negative bucket noise
        return GridSample(
            variable="rainfall_mm",
            lats=tiles["lats"],
            lons=tiles["lons"],
            values=values,  # APCP kg m-2 == mm
            grid_elevation_m=None,
            cycle=cycle,
            lead_hours=leads[-1],
            valid_time=valid,
            resolution_deg=tiles["resolution_deg"],
        )

    # -- earth2studio call + tile slicing ------------------------------------------

    def _fetch_tiles(
        self,
        native_vars: list[str],
        cycle: datetime,
        leads: list[float],
        lat: float,
        lon: float,
    ) -> dict:
        """Fetch native variables at ``cycle`` (+leads) and slice tiles around
        (lat, lon). Returns {var: 2-D tile, [var_prev,] lats, lons,
        resolution_deg}; coordinates ascending, lons in [-180, 180)."""
        cycle_naive = cycle.replace(tzinfo=None)  # e2s wants naive-UTC datetimes
        use_forecast = any(lead > 0 for lead in leads)
        if use_forecast:
            da = self._client("forecast")(
                time=[cycle_naive],
                lead_time=[timedelta(hours=h) for h in leads],
                variable=list(native_vars),
            )
        else:
            da = self._client("analysis")(time=[cycle_naive], variable=list(native_vars))

        out: dict = {}
        for vi, var in enumerate(native_vars):
            for li, lead in enumerate(leads):
                if use_forecast:
                    field = da.isel(time=0, lead_time=li).sel(variable=var)
                else:
                    field = da.isel(time=0).sel(variable=var)
                tile, lats, lons, res = _slice_tile(field, lat, lon)
                is_last_lead = li == len(leads) - 1
                key = var if is_last_lead else f"{var}_prev"
                out[key] = tile
                if vi == 0 and is_last_lead:
                    out["lats"], out["lons"], out["resolution_deg"] = lats, lons, res
        return out


def _slice_tile(field, lat: float, lon: float):
    """Slice a ~6x6 tile around (lat, lon) from a 2-D xarray field.

    Handles regular grids (1-D lat/lon coords, e.g. GFS/ERA5) and curvilinear
    grids (2-D coord arrays, e.g. HRRR Lambert-conformal).
    """
    coords = field.coords
    lat_name = "lat" if "lat" in coords else "latitude"
    lon_name = "lon" if "lon" in coords else "longitude"
    lat_arr = np.asarray(coords[lat_name].values, dtype=float)
    lon_arr = np.asarray(coords[lon_name].values, dtype=float)
    values = np.asarray(field.values, dtype=float)

    if lat_arr.ndim == 1:
        lon_norm = np.array([normalize_lon(x) for x in lon_arr])
        if lat_arr[0] > lat_arr[-1]:  # north->south storage: flip ascending
            lat_arr = lat_arr[::-1]
            values = values[::-1, :]
        order = np.argsort(lon_norm)
        lon_norm = lon_norm[order]
        values = values[:, order]
        j = int(np.clip(np.searchsorted(lat_arr, lat), _TILE_HALF, len(lat_arr) - _TILE_HALF))
        i = int(np.clip(np.searchsorted(lon_norm, lon), _TILE_HALF, len(lon_norm) - _TILE_HALF))
        jsl = slice(j - _TILE_HALF, j + _TILE_HALF)
        isl = slice(i - _TILE_HALF, i + _TILE_HALF)
        tile_lats, tile_lons = lat_arr[jsl], lon_norm[isl]
        tile = values[jsl, isl]
        res = float(np.median(np.diff(lat_arr)))
        return tile, tile_lats, tile_lons, abs(res)

    # Curvilinear: 2-D coordinate arrays.
    lon2d = np.vectorize(normalize_lon)(lon_arr)
    coslat = np.cos(np.radians(lat))
    d2 = (lat_arr - lat) ** 2 + (coslat * (lon2d - lon)) ** 2
    jn, in_ = np.unravel_index(int(np.argmin(d2)), d2.shape)
    nj, ni = lat_arr.shape
    j0 = int(np.clip(jn - _TILE_HALF, 0, nj - 2 * _TILE_HALF))
    i0 = int(np.clip(in_ - _TILE_HALF, 0, ni - 2 * _TILE_HALF))
    jsl, isl = slice(j0, j0 + 2 * _TILE_HALF), slice(i0, i0 + 2 * _TILE_HALF)
    tile_lats = lat_arr[jsl, isl]
    tile_lons = lon2d[jsl, isl]
    tile = values[jsl, isl]
    # Approximate spacing from the tile's latitude step.
    res = float(np.median(np.abs(np.diff(tile_lats, axis=0))))
    return tile, tile_lats, tile_lons, res


def build_earth2_sources(names: list[str]) -> dict[str, Earth2Source]:
    return {n: Earth2Source(n) for n in names if n in {"hrrr", "hrrr_fx", "gfs", "gfs_fx", "era5"}}
