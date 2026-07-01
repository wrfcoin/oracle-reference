"""oracle-reference — independent weather reference-verification service.

Serves gridded reference values, residuals, and location-plausibility checks
for the WRFCoin oracle (core4#724). Reference data only — the residual→tier
mapping is consensus-adjacent and lives in core4 shared-protocol, never here.
"""

from dataclasses import asdict

from fastapi import FastAPI

from app.config import settings
from app.sources import registry

app = FastAPI(
    title="oracle-reference",
    description="WRFCoin weather reference-verification service (earth2studio data sources, CPU-only)",
    version="0.1.0",
)


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
