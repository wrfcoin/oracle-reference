"""oracle-reference — independent weather reference-verification service.

Serves gridded reference values, residuals, and location-plausibility checks
for the WRFCoin oracle (core4#724). Reference data only — the residual→tier
mapping is consensus-adjacent and lives in core4 shared-protocol, never here.
"""

from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import FastAPI

from app.config import settings
from app.routes.v1 import router as v1_router
from app.sources import registry


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.sources.factory import build_sources  # noqa: PLC0415

    registry.attach(build_sources())
    yield


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
    }
