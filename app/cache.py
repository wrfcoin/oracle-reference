"""Cache janitor: keep the on-disk grid cache inside CACHE_MAX_GB.

Two passes over ``settings.cache_dir``:
1. age pass — delete files older than their subdir's retention (HRRR 7 d,
   GFS 14 d, MRMS 48 h; ERA5/ARCO subsets are pinned and never age out);
2. LRU pass — if the total is still over budget, delete non-pinned files
   oldest-access-first until under.

Subdir names match what the adapters/earth2studio actually write under
EARTH2STUDIO_CACHE (confirmed empirically: hrrr, gfs, arco, mrms — the FX
variants share their base source's subdir). Anything in an unknown subdir
falls under DEFAULT_MAX_AGE so junk can never accumulate unbounded.

Pure os.walk/os.stat/os.remove — unit-testable against tmp_path with faked
mtimes. Wired into the app lifespan as a periodic background task.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from app.config import settings

logger = logging.getLogger("oracle-reference.cache")

# subdir -> max age (None = pinned, never age-deleted, never LRU-evicted)
RETENTION: dict[str, timedelta | None] = {
    "hrrr": timedelta(days=7),
    "gfs": timedelta(days=14),
    "mrms": timedelta(hours=48),
    "arco": None,  # pinned ERA5 subsets (plausibility/retro windows)
}
DEFAULT_MAX_AGE = timedelta(days=14)


@dataclass
class JanitorStats:
    bytes_before: int = 0
    bytes_after: int = 0
    files_deleted: int = 0


class CacheJanitor:
    def __init__(self, cache_dir: str | None = None, max_gb: float | None = None):
        self.cache_dir = Path(cache_dir or settings.cache_dir)
        self.max_bytes = int((max_gb if max_gb is not None else settings.cache_max_gb) * 1e9)

    def run_once(self, now: float | None = None) -> JanitorStats:
        now = time.time() if now is None else now
        stats = JanitorStats()
        if not self.cache_dir.is_dir():
            return stats

        entries = self._scan()
        stats.bytes_before = sum(e[1] for e in entries)

        # Pass 1: age-based per-subdir retention.
        kept = []
        for path, size, mtime, atime, pinned, max_age in entries:
            if (
                max_age is not None
                and now - mtime > max_age.total_seconds()
                and self._delete(path)
            ):
                stats.files_deleted += 1
                continue
            kept.append((path, size, mtime, atime, pinned))

        # Pass 2: LRU down to budget (pinned files are exempt).
        total = sum(e[1] for e in kept)
        if total > self.max_bytes:
            evictable = sorted(
                (e for e in kept if not e[4]), key=lambda e: e[3]
            )  # oldest atime first
            for path, size, _mtime, _atime, _pinned in evictable:
                if total <= self.max_bytes:
                    break
                if self._delete(path):
                    stats.files_deleted += 1
                    total -= size
            if total > self.max_bytes:
                logger.warning(
                    "pinned cache alone exceeds budget: %.2f GB > %.2f GB",
                    total / 1e9,
                    self.max_bytes / 1e9,
                )
        stats.bytes_after = min(total, stats.bytes_before)
        return stats

    def _scan(self) -> list[tuple[Path, int, float, float, bool, timedelta | None]]:
        out = []
        for root, _dirs, files in os.walk(self.cache_dir):
            rel = Path(root).relative_to(self.cache_dir)
            subdir = rel.parts[0] if rel.parts else ""
            max_age = RETENTION.get(subdir, DEFAULT_MAX_AGE)
            pinned = subdir in RETENTION and RETENTION[subdir] is None
            for name in files:
                path = Path(root) / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                out.append((path, st.st_size, st.st_mtime, st.st_atime, pinned, max_age))
        return out

    def _delete(self, path: Path) -> bool:
        try:
            path.unlink()
            return True
        except OSError as exc:
            logger.warning("janitor could not delete %s: %s", path, exc)
            return False
