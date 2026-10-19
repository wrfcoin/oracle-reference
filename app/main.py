"""oracle-reference — independent weather reference-verification service.

Serves gridded reference values, residuals, and location-plausibility checks
for the WRFCoin oracle (core4#724). Reference data only — the residual→tier
mapping is consensus-adjacent and lives in core4 shared-protocol, never here.
"""

import logging
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import FastAPI

from app.config import settings
from app.routes.v1 import router as v1_router
from app.sources import registry


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    from app.cache import CacheJanitor
    from app.sources.factory import build_sources

    registry.attach(build_sources())

    async def janitor_loop() -> None:
        janitor = CacheJanitor()
        while True:
            stats = await asyncio.to_thread(janitor.run_once)
            if stats.files_deleted:
                logging.getLogger("oracle-reference").info(
                    "cache janitor: deleted %d files (%.2f -> %.2f GB)",
                    stats.files_deleted,
                    stats.bytes_before / 1e9,
                    stats.bytes_after / 1e9,
                )
            registry.refresh_cached_cycles()
            await asyncio.sleep(settings.janitor_interval_s)

    task = asyncio.create_task(janitor_loop()) if settings.janitor_interval_s > 0 else None
    yield
    if task is not None:
        task.cancel()


app = FastAPI(
    title="oracle-reference",
    description="WRFCoin weather reference-verification service (earth2studio data sources, CPU-only)",
    version="0.2.0",
    lifespan=lifespan,
)
app.include_router(v1_router)


@app.get("/healthz")
def healthz() -> dict:
    return {
        "status": "ok",
        "cached_cycles": registry.cached_cycles(),
        "uptime_s": round(registry.uptime_s(), 1),
    }


@app.get("/v1/sources/status")
def sources_status() -> dict:
    return {
        "sources": [asdict(s) for s in registry.statuses()],
        "cache_dir": settings.cache_dir,
        "cache_max_gb": settings.cache_max_gb,
        "cache_used_gb": registry.cache_used_gb(),
    }
