"""Build the enabled ReferenceSource adapters from settings."""

from __future__ import annotations

from app.config import settings
from app.sources.base import ReferenceSource


def build_sources(enabled: list[str] | None = None) -> dict[str, ReferenceSource]:
    """Adapters for every enabled source. Construction is cheap and safe under
    slim deps (earth2studio/cfgrib imports stay lazy inside fetch paths)."""
    from app.sources.earth2 import build_earth2_sources
    from app.sources.mrms import MRMSSource

    names = settings.enabled_sources if enabled is None else enabled
    sources: dict[str, ReferenceSource] = dict(build_earth2_sources(names))
    if "mrms" in names:
        sources["mrms"] = MRMSSource()
    return sources
