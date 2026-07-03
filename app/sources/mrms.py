"""MRMS 1-hour QPE adapter (rainfall_mm only).

MRMS is not shipped by earth2studio, so this adapter fetches directly from
the NOAA Open Data S3 bucket and decodes with cfgrib:

    https://noaa-mrms-pds.s3.amazonaws.com/CONUS/
        MultiSensor_QPE_01H_Pass2_00.00/{YYYYMMDD}/
        MRMS_MultiSensor_QPE_01H_Pass2_00.00_{YYYYMMDD}-{HH}0000.grib2.gz

MultiSensor Pass-2 is the best-quality 1-h QPE (~1 h latency). Values are mm
accumulated over the hour ending at the file timestamp; negative values are
no-coverage sentinels and become NaN. cfgrib/eccodes are imported lazily so
the module imports under slim CI deps.
"""

from __future__ import annotations

import gzip
import shutil
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from app.config import settings
from app.interp import normalize_lon
from app.sources.base import GridSample, SourceFetchError

_BUCKET = "https://noaa-mrms-pds.s3.amazonaws.com"
_PRODUCT = "MultiSensor_QPE_01H_Pass2_00.00"
_TILE_HALF_DEG = 0.06  # ~6x6 nodes on the 0.01-degree MRMS grid


class MRMSSource:
    name = "mrms"
    variables = frozenset({"rainfall_mm"})
    max_lead_hours = 0.0

    def __init__(self) -> None:
        self._cache = Path(settings.cache_dir) / self.cache_subdir()

    def cache_subdir(self) -> str:
        return "mrms"

    def fetch(self, time: datetime, variable: str, lat: float, lon: float) -> GridSample:
        if variable not in self.variables:
            raise SourceFetchError(f"mrms serves rainfall_mm only, not {variable}")
        valid = time.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        path = self._ensure_file(valid)
        lats, lons, values = self._decode_tile(path, lat, lon)
        return GridSample(
            variable="rainfall_mm",
            lats=lats,
            lons=lons,
            values=values,
            grid_elevation_m=None,  # radar-derived product; no model orography
            cycle=valid,
            lead_hours=0.0,
            valid_time=valid,
            resolution_deg=0.01,
        )

    def latest_cycle(self) -> datetime | None:
        files = sorted(self._cache.glob("MRMS_*.grib2")) if self._cache.is_dir() else []
        if not files:
            return None
        stamp = files[-1].stem.rsplit("_", 1)[-1]  # YYYYMMDD-HHMMSS
        return datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)

    def _url(self, valid: datetime) -> str:
        day = valid.strftime("%Y%m%d")
        stamp = valid.strftime("%Y%m%d-%H0000")
        return f"{_BUCKET}/CONUS/{_PRODUCT}/{day}/MRMS_{_PRODUCT}_{stamp}.grib2.gz"

    def _ensure_file(self, valid: datetime) -> Path:
        self._cache.mkdir(parents=True, exist_ok=True)
        out = self._cache / f"MRMS_{_PRODUCT}_{valid.strftime('%Y%m%d-%H0000')}.grib2"
        if out.exists():
            return out
        url = self._url(valid)
        tmp_gz = out.with_suffix(".grib2.gz.part")
        try:
            with urllib.request.urlopen(url, timeout=settings.fetch_timeout_s) as resp:
                with open(tmp_gz, "wb") as fh:
                    shutil.copyfileobj(resp, fh)
            with gzip.open(tmp_gz, "rb") as gz, open(out, "wb") as fh:
                shutil.copyfileobj(gz, fh)
        except Exception as exc:
            raise SourceFetchError(f"mrms fetch failed for {url}: {exc}") from exc
        finally:
            tmp_gz.unlink(missing_ok=True)
        return out

    def _decode_tile(
        self, path: Path, lat: float, lon: float
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        return decode_grib_tile(path, lat, lon, _TILE_HALF_DEG)


def decode_grib_tile(
    path: Path, lat: float, lon: float, half_deg: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Decode one GRIB2 message and slice a tile around (lat, lon).

    Returns ascending 1-D lats/lons and the value tile with negative
    no-coverage sentinels replaced by NaN. Requires cfgrib/eccodes
    (lazy import — exercised by the `grib`-marked test and live runs).
    """
    try:
        import xarray as xr  # noqa: PLC0415 - heavy import stays lazy
    except ImportError as exc:  # pragma: no cover - slim CI never calls this
        raise SourceFetchError(f"cfgrib/xarray unavailable for GRIB decode: {exc}") from exc
    try:
        ds = xr.open_dataset(path, engine="cfgrib", decode_timedelta=False)
    except Exception as exc:
        raise SourceFetchError(f"GRIB decode failed for {path.name}: {exc}") from exc
    try:
        da = ds[next(iter(ds.data_vars))]
        grid_lats = np.asarray(da.latitude.values, dtype=float)
        grid_lons = np.array([normalize_lon(x) for x in np.asarray(da.longitude.values, dtype=float)])
        values = np.asarray(da.values, dtype=float)
    finally:
        ds.close()
    if grid_lats[0] > grid_lats[-1]:  # MRMS stores north→south; flip ascending
        grid_lats = grid_lats[::-1]
        values = values[::-1, :]
    order = np.argsort(grid_lons)
    grid_lons = grid_lons[order]
    values = values[:, order]
    jsel = (grid_lats >= lat - half_deg) & (grid_lats <= lat + half_deg)
    isel = (grid_lons >= lon - half_deg) & (grid_lons <= lon + half_deg)
    if jsel.sum() < 2 or isel.sum() < 2:
        raise SourceFetchError(f"point ({lat}, {lon}) outside grid coverage of {path.name}")
    tile = values[np.ix_(jsel, isel)].copy()
    tile[tile < 0] = np.nan  # no-coverage sentinels
    return grid_lats[jsel], grid_lons[isel], tile
