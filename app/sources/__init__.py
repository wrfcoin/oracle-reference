"""Data-source registry.

Adapters register themselves here behind the source-agnostic
``ReferenceSource`` protocol (app/sources/base.py) so the earth2studio
dependency stays swappable (e.g. Herbie/cfgrib) without touching routes.
Sources with no attached adapter report ``unavailable`` with a reason.
"""

import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.config import settings
from app.sources.base import ReferenceSource


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
    _sources: dict[str, ReferenceSource] = field(default_factory=dict)
    started_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        for name in settings.enabled_sources:
            self._statuses[name] = SourceStatus(
                name=name,
                available=False,
                detail="no adapter attached",
            )

    def attach(self, sources: dict[str, ReferenceSource]) -> None:
        """Attach adapters (factory-built at startup; FixtureSources in tests)."""
        self._sources = dict(sources)
        for name, src in self._sources.items():
            status = self._statuses.setdefault(name, SourceStatus(name=name, available=False))
            status.available = True
            status.detail = f"adapter attached ({type(src).__name__})"
        self.refresh_cached_cycles()

    def sources(self) -> dict[str, ReferenceSource]:
        return self._sources

    def source(self, name: str) -> Optional[ReferenceSource]:
        return self._sources.get(name)

    def record_fetch(self, name: str, cycle: datetime, latency_s: float) -> None:
        status = self._statuses.get(name)
        if status is None:
            return
        status.available = True
        status.latest_cycle = cycle.isoformat()
        status.last_fetch_latency_s = round(latency_s, 3)
        status.detail = ""

    def record_error(self, name: str, detail: str) -> None:
        status = self._statuses.get(name)
        if status is None:
            return
        status.error_count += 1
        status.detail = detail[:200]

    def refresh_cached_cycles(self) -> None:
        """Report real cached cycles by asking each adapter (fixture/memoized
        state, cache-dir scans — adapter's choice; never a network fetch)."""
        for name, src in self._sources.items():
            status = self._statuses.get(name)
            if status is None:
                continue
            try:
                cycle = src.latest_cycle()
            except Exception as exc:  # pragma: no cover - defensive
                self.record_error(name, f"latest_cycle failed: {exc}")
                continue
            if cycle is not None:
                status.latest_cycle = cycle.isoformat()

    def cache_used_gb(self) -> float:
        total = 0
        for root, _dirs, files in os.walk(settings.cache_dir):
            for f in files:
                try:
                    total += os.stat(os.path.join(root, f)).st_size
                except OSError:
                    continue
        return round(total / 1e9, 3)

    def statuses(self) -> list[SourceStatus]:
        return list(self._statuses.values())

    def cached_cycles(self) -> dict[str, Optional[str]]:
        return {s.name: s.latest_cycle for s in self._statuses.values()}

    def uptime_s(self) -> float:
        return time.monotonic() - self.started_at


registry = SourceRegistry()
