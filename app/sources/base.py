"""Source-agnostic adapter boundary.

``GridSample`` is a plain-numpy tile — everything above this boundary
(services, routes, tests) is free of earth2studio/xarray/cfgrib, so a
Herbie/cfgrib reimplementation of any source stays contained to its adapter
module, and the whole service layer is testable from recorded fixtures with
slim CI dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

import numpy as np


class SourceFetchError(RuntimeError):
    """Upstream fetch/decode failure for one (source, variable, time)."""


@dataclass
class GridSample:
    """A small grid tile around a station point, canonical units.

    ``lats``/``lons`` are either 1-D ascending (regular grids) or 2-D
    coordinate arrays (curvilinear grids, e.g. HRRR Lambert-conformal).
    Longitudes are normalized to [-180, 180).
    """

    variable: str  # canonical name, e.g. "temperature_c"
    lats: np.ndarray
    lons: np.ndarray
    values: np.ndarray  # canonical units
    grid_elevation_m: np.ndarray | None  # elevation datum of the values (0 for MSLP)
    cycle: datetime  # analysis/forecast cycle (UTC)
    lead_hours: float  # 0.0 for analysis
    valid_time: datetime
    resolution_deg: float
    # Vector components (e.g. wind u/v) so consumers can interpolate each
    # component and then take the magnitude; None for scalar variables.
    components: dict[str, np.ndarray] | None = None


@runtime_checkable
class ReferenceSource(Protocol):
    name: str
    variables: frozenset[str]  # canonical vars this source serves
    max_lead_hours: float  # 0 for analysis sources

    def fetch(self, time: datetime, variable: str, lat: float, lon: float) -> GridSample:
        """Tile of ``variable`` valid at ``time`` around (lat, lon).

        Raises SourceFetchError on upstream failure; never returns tiers.
        """
        ...

    def latest_cycle(self) -> datetime | None: ...

    def cache_subdir(self) -> str:
        """Subdirectory under settings.cache_dir this source writes to
        (drives the janitor's retention policy and status scanning)."""
        ...
