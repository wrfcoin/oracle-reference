"""Pydantic request/response models for the v1 API.

Canonical variables and units match core4 shared-protocol
(temperature_c °C, pressure_hpa hPa, humidity_pct %, wind_speed_ms m/s,
rainfall_mm mm). Tier metadata is advisory and mirrors the pinned core4 policy; it does not
change attestation confidence or consensus rewards.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.bands import BANDS, BANDS_VERSION, CANONICAL_VARIABLES
from app.tiering import FOLD_RULE, FoldRule, Tier, gross_guard_active_at

SERVICE_VERSION = "0.2.0"


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware (UTC)")
    return value.astimezone(UTC)


class ServiceInfo(BaseModel):
    version: str = SERVICE_VERSION
    bands_version: int = BANDS_VERSION


# ---------------------------------------------------------------- /v1/reference


class PointQuery(BaseModel):
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, lt=360.0)  # 0-360 accepted, normalized internally
    elevation_m: float | None = Field(default=None, ge=-430.0, le=9000.0)
    time: datetime

    @field_validator("time")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return _require_utc(v)


class ReferenceRequest(BaseModel):
    points: list[PointQuery] = Field(min_length=1, max_length=100)
    variables: list[str] | None = None  # default: all canonical
    sources: list[str] | None = None  # default: all enabled serving >=1 var

    @field_validator("variables")
    @classmethod
    def _known_vars(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            unknown = set(v) - set(CANONICAL_VARIABLES)
            if unknown:
                raise ValueError(f"unknown variables: {sorted(unknown)}")
            if not v:
                raise ValueError("variables must be non-empty when given")
        return v

    @field_validator("sources")
    @classmethod
    def _known_sources(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            unknown = set(v) - set(BANDS)
            if unknown:
                raise ValueError(f"unknown sources: {sorted(unknown)}")
        return v


class GridMeta(BaseModel):
    nearest_distance_km: float
    # Elevation datum of the uncorrected pressure value (0.0 = MSLP). The
    # models expose no usable orography; pressure is served as MSLP corrected
    # hypsometrically to the station elevation, so grid-vs-station elevation
    # error never enters the residual.
    grid_elevation_m: float | None = None
    resolution_deg: float
    elevation_correction_applied: dict[str, bool] = Field(default_factory=dict)


class SourceValues(BaseModel):
    source: str
    cycle: datetime | None = None
    lead_hours: float | None = None  # max over returned variables; 0 = analysis
    leads: dict[str, float] = Field(default_factory=dict)  # per-variable lead_hours
    values: dict[str, float] = Field(default_factory=dict)
    grid: GridMeta | None = None
    error: str | None = None


class PointResult(BaseModel):
    point: PointQuery
    sources: list[SourceValues]


class ReferenceResponse(BaseModel):
    results: list[PointResult]
    service: ServiceInfo = Field(default_factory=ServiceInfo)


# ---------------------------------------------------------------- /v1/residuals


class Observation(BaseModel):
    station_id: str | None = None
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, lt=360.0)
    elevation_m: float | None = Field(default=None, ge=-430.0, le=9000.0)
    time: datetime | None = None
    observed_at_unix: int | None = None  # seconds or ms (disambiguated by magnitude)
    observation: dict[str, float] = Field(default_factory=dict)

    # Canonical variables may also arrive flattened at the top level (core4
    # canonical reading shape) — folded into `observation` here.
    model_config = {"extra": "allow"}

    @field_validator("time")
    @classmethod
    def _tz(cls, v: datetime | None) -> datetime | None:
        return _require_utc(v) if v is not None else None

    def resolved_time(self) -> datetime:
        if self.time is not None:
            return self.time
        if self.observed_at_unix is not None:
            ts = self.observed_at_unix
            if ts > 10**12:  # milliseconds
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts, tz=UTC)
        raise ValueError("observation needs `time` or `observed_at_unix`")

    def resolved_variables(self) -> dict[str, float]:
        merged = dict(self.observation)
        extra = self.model_extra or {}
        for var in CANONICAL_VARIABLES:
            if var in extra and isinstance(extra[var], (int, float)):
                merged.setdefault(var, float(extra[var]))
        unknown = set(merged) - set(CANONICAL_VARIABLES)
        if unknown:
            raise ValueError(f"unknown observation variables: {sorted(unknown)}")
        return merged


class TieringContext(BaseModel):
    # Caller supplies the chain schedule context; this service does not guess
    # activation from wall time or call a live node.
    height: int = Field(strict=True, ge=0, le=2**64 - 1)
    gross_guard_activation_height: int | None = Field(default=None, strict=True, ge=0, le=2**64 - 1)

    def gross_guard_active(self) -> bool:
        return gross_guard_active_at(self.height, self.gross_guard_activation_height)


class ResidualsRequest(BaseModel):
    tiering_context: TieringContext | None = None
    observations: list[Observation] = Field(min_length=1, max_length=100)
    sources: list[str] | None = None
    # Which space the top-level marginal/outlier thresholds are expressed in;
    # it must match how the consumer compares:
    # - "raw" (default): compare max(|residuals|) (canonical units) against
    #   per-result scaled thresholds — the attestation loop's reference_check
    #   works verbatim, and the decision is exactly the band-ratio decision.
    # - "ratio": compare max(band_ratios) against the constants 1.0 / 2.0
    #   (the stable dimensionless contract).
    mode: Literal["raw", "ratio"] = "raw"

    @field_validator("sources")
    @classmethod
    def _sources_known(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            unknown = set(v) - set(BANDS)
            if unknown:
                raise ValueError(f"unknown sources: {sorted(unknown)}")
        return v


class SourceResiduals(BaseModel):
    source: str
    cycle: datetime | None = None
    lead_hours: float | None = None
    residuals: dict[str, float] = Field(default_factory=dict)  # observed - reference, canonical units
    band_ratios: dict[str, float] = Field(default_factory=dict)  # |residual| / marginal_max (bands v3)
    reference: dict[str, float] = Field(default_factory=dict)
    grid: GridMeta | None = None
    error: str | None = None


class WorstResidual(BaseModel):
    source: str
    variable: str
    residual: float
    band_ratio: float


class ResidualResult(BaseModel):
    station_id: str | None = None
    per_source: list[SourceResiduals]
    worst_ratio: float | None = None
    worst: WorstResidual | None = None
    marginal_threshold: float
    outlier_threshold: float
    bands_version: int = BANDS_VERSION
    tier: Tier
    fold_rule: FoldRule = FOLD_RULE
    gross_guard_active: bool = False


class ResidualsResponse(BaseModel):
    results: list[ResidualResult]
    service: ServiceInfo = Field(default_factory=ServiceInfo)


# ---------------------------------------------------- /v1/location-plausibility


class ClaimedLocation(BaseModel):
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, lt=360.0)
    elevation_m: float | None = Field(default=None, ge=-430.0, le=9000.0)


class SeriesSample(BaseModel):
    time: datetime
    observation: dict[str, float]

    @field_validator("time")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return _require_utc(v)


class PlausibilityOptions(BaseModel):
    # Box-series capable analysis sources (regular grids with hourly archives).
    reference_source: Literal["era5", "gfs"] = "era5"
    search_radius_deg: float = Field(default=6.0, gt=0.0, le=15.0)
    search_step_deg: float = Field(default=0.5, gt=0.05, le=2.0)
    resample_hours: int = Field(default=3, ge=1, le=6)


class PlausibilityRequest(BaseModel):
    claimed: ClaimedLocation
    series: list[SeriesSample] = Field(min_length=1, max_length=10000)
    options: PlausibilityOptions = Field(default_factory=PlausibilityOptions)


class ElevationCheck(BaseModel):
    implied_elevation_m: float | None = None
    claimed_elevation_m: float | None = None
    delta_m: float | None = None
    n_samples: int = 0
    score: float | None = None
    available: bool = False


class CorrelationCheck(BaseModel):
    corr_at_claimed: float | None = None
    best_corr: float | None = None
    best_match: dict[str, float] | None = None  # {lat, lon}
    best_match_distance_km: float | None = None
    grid_step_deg: float | None = None
    score: float | None = None
    available: bool = False


class SeriesInfo(BaseModel):
    n_samples_in: int
    n_samples_used: int
    start: datetime | None = None
    end: datetime | None = None
    variables_used: list[str] = Field(default_factory=list)


class PlausibilityResponse(BaseModel):
    verdict: Literal["consistent", "suspect", "inconsistent"]
    plausibility: float
    checks: dict[str, ElevationCheck | CorrelationCheck]
    series: SeriesInfo
    reference_source: str
    limitations: str = (
        "Location resolution is ~one reference grid cell (10-25 km); "
        "verdicts localize region, not address."
    )
    service: ServiceInfo = Field(default_factory=ServiceInfo)
