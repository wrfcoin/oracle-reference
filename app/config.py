"""Service configuration from environment variables."""

import os
from dataclasses import dataclass, field


def _sources_from_env() -> list[str]:
    raw = os.environ.get("ENABLED_SOURCES", "hrrr,hrrr_fx,gfs,gfs_fx,mrms,era5")
    return [s.strip() for s in raw.split(",") if s.strip()]


@dataclass(frozen=True)
class Settings:
    port: int = int(os.environ.get("PORT", "8010"))
    cache_dir: str = os.environ.get("EARTH2STUDIO_CACHE", "/data/cache")
    cache_max_gb: float = float(os.environ.get("CACHE_MAX_GB", "10"))
    enabled_sources: list[str] = field(default_factory=_sources_from_env)
    # Timeout for a single upstream fetch (byte-ranged GRIB from NOAA S3 etc.)
    fetch_timeout_s: float = float(os.environ.get("FETCH_TIMEOUT_S", "120"))


settings = Settings()
