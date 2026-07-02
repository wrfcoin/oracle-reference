"""v1 API routes. Endpoints are sync `def` on purpose: FastAPI runs them in
its threadpool, so blocking upstream I/O in adapters never stalls the loop."""

from __future__ import annotations

from fastapi import APIRouter

from app.models import (
    PlausibilityRequest,
    PlausibilityResponse,
    ReferenceRequest,
    ReferenceResponse,
    ResidualsRequest,
    ResidualsResponse,
)
from app.services.plausibility import check_plausibility
from app.services.reference import compute_reference
from app.services.residuals import compute_residuals

router = APIRouter(prefix="/v1")


@router.post("/reference", response_model=ReferenceResponse)
def reference(req: ReferenceRequest) -> ReferenceResponse:
    results = compute_reference(req.points, req.variables, req.sources)
    return ReferenceResponse(results=results)


@router.post("/residuals", response_model=ResidualsResponse)
def residuals(req: ResidualsRequest) -> ResidualsResponse:
    return ResidualsResponse(results=compute_residuals(req))


@router.post("/location-plausibility", response_model=PlausibilityResponse)
def location_plausibility(req: PlausibilityRequest) -> PlausibilityResponse:
    return check_plausibility(req)
