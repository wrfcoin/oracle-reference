#!/usr/bin/env python3
"""Record grid fixtures for the test suite. Dev-machine only: needs network
and the full dependency stack (CPU torch -> earth2studio, cfgrib).

    python scripts/record_fixtures.py --out tests/fixtures [--era5-box] [--mrms-grib]

Fixtures are pinned to a historical timestamp chosen inside every upstream
archive's retention window (NOAA S3 HRRR/GFS/MRMS, ARCO ERA5). Each tile is a
few-KB .npz; the manifest carries the GridSample metadata. Re-running the
script regenerates fixtures reproducibly from the same pinned inputs.
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.sources.factory import build_sources  # noqa: E402
from app.sources.fixture_source import fixture_sources  # noqa: E402

DULUTH = (46.8369, -92.1833)  # station point; elevation ~186 m (lakeshore)
# Pinned recording time: within ARCO ERA5 coverage and the NOAA S3 archives.
PINNED = datetime(2024, 6, 24, 12, tzinfo=timezone.utc)
# 7-day window for the plausibility box fixture (3-hourly).
BOX_START = datetime(2024, 6, 17, 0, tzinfo=timezone.utc)
BOX_STEPS = 56
BOX_STEP_HOURS = 3
BOX_HALF_DEG = 6.0
BOX_STEP_DEG = 0.5


def record_tiles(out_dir: Path) -> None:
    manifest: list[dict] = []
    sources = build_sources()
    for name, src in sorted(sources.items()):
        for var in sorted(src.variables):
            print(f"recording {name}/{var} @ {PINNED.isoformat()} ...", flush=True)
            sample = src.fetch(PINNED, var, *DULUTH)
            fname = f"{name}_{var}_duluth.npz"
            arrays: dict[str, np.ndarray] = {
                "lats": sample.lats,
                "lons": sample.lons,
                "values": sample.values,
            }
            if sample.grid_elevation_m is not None:
                arrays["grid_elevation_m"] = sample.grid_elevation_m
            if sample.components is not None:
                arrays["component_u"] = sample.components["u"]
                arrays["component_v"] = sample.components["v"]
            np.savez_compressed(out_dir / fname, **arrays)
            manifest.append(
                {
                    "file": fname,
                    "source": name,
                    "variable": var,
                    "cycle": sample.cycle.isoformat(),
                    "lead_hours": sample.lead_hours,
                    "valid_time": sample.valid_time.isoformat(),
                    "resolution_deg": sample.resolution_deg,
                }
            )
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    # Round-trip check: every entry loads back through FixtureSource.
    for name, src in fixture_sources(out_dir).items():
        for var in src.variables:
            loaded = src.fetch(PINNED, var, *DULUTH)
            assert loaded.values.size > 0, (name, var)
    print(f"wrote {len(manifest)} tiles + manifest to {out_dir}")


def record_era5_box(out_dir: Path) -> None:
    """t2m + msl box series around Duluth for the plausibility grid-search:
    (BOX_STEPS, ny, nx) float32, plus a Duluth point series used to derive
    the synthetic station fixture."""
    import earth2studio.data as e2d  # noqa: PLC0415

    times = [BOX_START + timedelta(hours=BOX_STEP_HOURS * i) for i in range(BOX_STEPS)]
    lat0, lon0 = DULUTH
    lats = np.arange(lat0 - BOX_HALF_DEG, lat0 + BOX_HALF_DEG + 1e-9, BOX_STEP_DEG)
    lons = np.arange(lon0 - BOX_HALF_DEG, lon0 + BOX_HALF_DEG + 1e-9, BOX_STEP_DEG)
    arco = e2d.ARCO(cache=True, verbose=False)
    da = arco(time=[t.replace(tzinfo=None) for t in times], variable=["t2m", "msl"])
    da = da.sel(lat=lats, lon=(lons % 360.0), method="nearest")
    t2m = da.sel(variable="t2m").values.astype(np.float32)  # (time, ny, nx), K
    msl = da.sel(variable="msl").values.astype(np.float32)  # (time, ny, nx), Pa
    np.savez_compressed(
        out_dir / "era5_box_duluth.npz",
        times=np.array([t.isoformat() for t in times]),
        lats=lats.astype(np.float32),
        lons=lons.astype(np.float32),
        t2m_k=t2m,
        msl_pa=msl,
    )
    print(f"wrote era5_box_duluth.npz {t2m.shape} to {out_dir}")


def record_mrms_grib(out_dir: Path) -> None:
    """Keep one real (gzipped) MRMS GRIB2 for the decode test, if small enough."""
    from app.sources.mrms import MRMSSource  # noqa: PLC0415

    src = MRMSSource()
    path = src._ensure_file(PINNED)
    gz_path = out_dir / "mrms_sample.grib2.gz"
    with open(path, "rb") as fh, gzip.open(gz_path, "wb", compresslevel=9) as gz:
        shutil.copyfileobj(fh, gz)
    size_mb = gz_path.stat().st_size / 1e6
    if size_mb > 2.0:
        gz_path.unlink()
        print(f"mrms grib too large to check in ({size_mb:.1f} MB) — skipped")
    else:
        print(f"wrote mrms_sample.grib2.gz ({size_mb:.2f} MB)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("tests/fixtures"))
    parser.add_argument("--era5-box", action="store_true")
    parser.add_argument("--mrms-grib", action="store_true")
    parser.add_argument("--skip-tiles", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if not args.skip_tiles:
        record_tiles(args.out)
    if args.era5_box:
        record_era5_box(args.out)
    if args.mrms_grib:
        record_mrms_grib(args.out)
