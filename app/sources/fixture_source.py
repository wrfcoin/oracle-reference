"""ReferenceSource backed by recorded .npz tiles.

Used by the test suite (slim deps, no network) and as the round-trip check
for scripts/record_fixtures.py. Fixture layout:

    <fixtures_dir>/manifest.json       — list of entries (see below)
    <fixtures_dir>/<entry["file"]>     — np.savez_compressed GridSample arrays

Manifest entry:
    {"file": "hrrr_temperature_c_duluth.npz", "source": "hrrr",
     "variable": "temperature_c", "cycle": "2026-06-24T12:00:00+00:00",
     "lead_hours": 0.0, "valid_time": "2026-06-24T12:00:00+00:00",
     "resolution_deg": 0.027}
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np

from app.sources.base import GridSample, SourceFetchError


def load_grid_sample(fixtures_dir: Path, entry: dict) -> GridSample:
    data = np.load(fixtures_dir / entry["file"])
    grid_elev = data["grid_elevation_m"] if "grid_elevation_m" in data.files else None
    components = None
    if "component_u" in data.files:
        components = {"u": data["component_u"], "v": data["component_v"]}
    return GridSample(
        variable=entry["variable"],
        lats=data["lats"],
        lons=data["lons"],
        values=data["values"],
        grid_elevation_m=grid_elev,
        cycle=datetime.fromisoformat(entry["cycle"]),
        lead_hours=float(entry["lead_hours"]),
        valid_time=datetime.fromisoformat(entry["valid_time"]),
        resolution_deg=float(entry["resolution_deg"]),
        components=components,
    )


class FixtureSource:
    """Serves recorded tiles for one source name, ignoring the request time
    (fixtures are pinned to their recorded valid_time)."""

    max_lead_hours = 0.0

    def __init__(self, name: str, fixtures_dir: Path):
        self.name = name
        self._dir = Path(fixtures_dir)
        manifest = json.loads((self._dir / "manifest.json").read_text())
        self._entries = {e["variable"]: e for e in manifest if e["source"] == name}
        self.variables = frozenset(self._entries)

    def fetch(self, time: datetime, variable: str, lat: float, lon: float) -> GridSample:
        entry = self._entries.get(variable)
        if entry is None:
            raise SourceFetchError(f"{self.name}: no recorded fixture for {variable}")
        return load_grid_sample(self._dir, entry)

    def latest_cycle(self) -> datetime | None:
        cycles = [datetime.fromisoformat(e["cycle"]) for e in self._entries.values()]
        return max(cycles) if cycles else None

    def cache_subdir(self) -> str:
        return self.name


def fixture_sources(fixtures_dir: Path) -> dict[str, FixtureSource]:
    """One FixtureSource per source present in the manifest."""
    manifest = json.loads((Path(fixtures_dir) / "manifest.json").read_text())
    names = sorted({e["source"] for e in manifest})
    return {n: FixtureSource(n, Path(fixtures_dir)) for n in names}
