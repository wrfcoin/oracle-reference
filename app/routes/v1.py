"""v1 API routes. Endpoints are sync `def` on purpose: FastAPI runs them in
its threadpool, so blocking upstream I/O in adapters never stalls the loop."""

from __future__ import annotations

from fastapi import APIRouter

from app.models import ReferenceRequest, ReferenceResponse
from app.services.reference import compute_reference

router = APIRouter(prefix="/v1")


@router.post("/reference", response_model=ReferenceResponse)
def reference(req: ReferenceRequest) -> ReferenceResponse:
    results = compute_reference(req.points, req.variables, req.sources)
    return ReferenceResponse(results=results)
