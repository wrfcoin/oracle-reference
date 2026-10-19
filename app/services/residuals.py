"""Residual computation: observations in → per-source residuals out.

``residuals`` are observed − reference in canonical units, sign preserved —
identical semantics to core4 ``ReferenceResidual`` (the backend#866 shadow
scorer consumes them). ``band_ratios`` express each residual as
|residual| / marginal_max from the embedded bands v3, so mixed-unit residuals
become comparable against the single top-level thresholds. Additive advisory tier metadata mirrors the pinned core4 rule without
changing the ratio thresholds or attestation confidence formula.
"""

from __future__ import annotations

from fastapi import HTTPException

from app import bands
from app.models import (
    Observation,
    PointQuery,
    ResidualResult,
    ResidualsRequest,
    SourceResiduals,
    WorstResidual,
)
from app.services.reference import compute_reference
from app.tiering import tier_residuals

# Ratio-space thresholds (mode="ratio"): a band ratio of 1.0 sits exactly on
# the marginal_max edge for every band in the table; 2.0 is the confidence-
# scaling constant (PR #6's confidence = clamp(1 - worst/outlier_threshold)
# gives 0.5 at the marginal edge and 0 at twice the marginal band).
RATIO_MARGINAL_THRESHOLD = 1.0
RATIO_OUTLIER_THRESHOLD = 2.0


def compute_residuals(req: ResidualsRequest) -> list[ResidualResult]:
    results = []
    for obs in req.observations:
        try:
            observed = obs.resolved_variables()
            when = obs.resolved_time()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not observed:
            raise HTTPException(status_code=422, detail="observation carries no canonical variables")
        point = PointQuery(lat=obs.lat, lon=obs.lon, elevation_m=obs.elevation_m, time=when)
        (ref_result,) = compute_reference([point], sorted(observed), req.sources)
        results.append(_one_result(obs, observed, ref_result.sources, req.mode,
                                   req.tiering_context.gross_guard_active() if req.tiering_context else False))
    return results


def _one_result(
    obs: Observation, observed: dict[str, float], ref_sources: list, mode: str,
    gross_guard_active: bool = False,
) -> ResidualResult:
    per_source: list[SourceResiduals] = []
    worst: WorstResidual | None = None
    for ref in ref_sources:
        residuals: dict[str, float] = {}
        ratios: dict[str, float] = {}
        for var, ref_value in ref.values.items():
            residual = round(observed[var] - ref_value, 4)
            ratio = bands.band_ratio(ref.source, var, residual)
            if ratio is None:  # no band for the pair -> skipped, mirroring core4
                continue
            residuals[var] = residual
            ratios[var] = round(ratio, 4)
            if worst is None or ratio > worst.band_ratio:
                worst = WorstResidual(
                    source=ref.source, variable=var, residual=residual, band_ratio=round(ratio, 4)
                )
        per_source.append(
            SourceResiduals(
                source=ref.source,
                cycle=ref.cycle,
                lead_hours=ref.lead_hours,
                residuals=residuals,
                band_ratios=ratios,
                reference={v: ref.values[v] for v in residuals},
                grid=ref.grid,
                error=ref.error,
            )
        )
    marginal, outlier = _thresholds(mode, per_source, worst)
    return ResidualResult(
        station_id=obs.station_id,
        tier=tier_residuals([{"source": s.source, "variable": v, "residual": r}
                             for s in per_source for v, r in s.residuals.items()],
                            gross_guard_active=gross_guard_active),
        gross_guard_active=gross_guard_active,
        per_source=per_source,
        worst_ratio=worst.band_ratio if worst else None,
        worst=worst,
        marginal_threshold=marginal,
        outlier_threshold=outlier,
    )


def _thresholds(
    mode: str, per_source: list[SourceResiduals], worst: WorstResidual | None
) -> tuple[float, float]:
    if mode == "ratio":
        return RATIO_MARGINAL_THRESHOLD, RATIO_OUTLIER_THRESHOLD
    # raw (default): per-RESULT thresholds scaled so that a consumer comparing
    # max(|raw residual|) against them (PR #6's reference_check, verbatim)
    # decides EXACTLY as the ratio comparison would:
    #   max_raw <= scale  <=>  worst_ratio <= 1
    #   1 - max_raw / (2 * scale) == 1 - worst_ratio / 2
    # These thresholds are per-result values, not constants — the ratio mode
    # is the stable dimensionless contract.
    if worst is None or worst.band_ratio == 0.0:
        return RATIO_MARGINAL_THRESHOLD, RATIO_OUTLIER_THRESHOLD  # nothing to gate
    max_raw = max(abs(r) for s in per_source for r in s.residuals.values())
    scale = max_raw / worst.band_ratio
    return scale, 2.0 * scale
