"""Data-source registry.

Adapters (Wave 2, issue #2) register themselves here behind a source-agnostic
interface so the earth2studio dependency stays swappable (e.g. Herbie/cfgrib)
without touching routes. Until adapters land, the registry reports every
enabled source as ``unavailable`` with a reason.
"""

import time
from dataclasses import dataclass, field
from typing import Optional

from app.config import settings


@dataclass
class SourceStatus:
    name: str
    available: bool
    latest_cycle: Optional[str] = None  # ISO time of newest analysis/forecast cycle
    last_fetch_latency_s: Optional[float] = None
    error_count: int = 0
    detail: str = ""


@dataclass
class SourceRegistry:
    _statuses: dict[str, SourceStatus] = field(default_factory=dict)
    started_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        for name in settings.enabled_sources:
            self._statuses[name] = SourceStatus(
                name=name,
                available=False,
                detail="adapter not yet implemented (Wave 2)",
            )

    def statuses(self) -> list[SourceStatus]:
        return list(self._statuses.values())

    def cached_cycles(self) -> dict[str, Optional[str]]:
        return {s.name: s.latest_cycle for s in self._statuses.values()}

    def uptime_s(self) -> float:
        return time.monotonic() - self.started_at


registry = SourceRegistry()
