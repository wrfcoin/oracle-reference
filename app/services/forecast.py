"""Point extraction from bounded, normalized GRIB-subset archives (schema v1)."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

import numpy as np
from fastapi import HTTPException
from pydantic import BaseModel, Field, StrictInt, field_validator

from app.config import settings
from app.interp import bilinear, normalize_lon
from app.models import ServiceInfo

Variable = Literal["temperature_c", "pressure_hpa", "humidity_pct", "wind_speed_ms", "rainfall_mm"]
Source = Literal["gfs_fx", "gefs_fx"]


class ForecastPoint(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, lt=360)


class ForecastRequest(BaseModel):
    source: Source
    cycle: datetime
    points: list[ForecastPoint] = Field(min_length=1, max_length=100)
    lead_hours: list[StrictInt] = Field(min_length=1, max_length=64)
    variables: list[Variable] = Field(default_factory=lambda: ["temperature_c"], min_length=1, max_length=5)
    members: list[str] | None = Field(default=None, min_length=1, max_length=31)

    @field_validator("cycle")
    @classmethod
    def _cycle(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("cycle must be timezone-aware")
        value = value.astimezone(timezone.utc)
        if value.hour % 6 or value.minute or value.second or value.microsecond:
            raise ValueError("cycle must be a six-hour UTC cycle")
        return value

    @field_validator("lead_hours", "variables", "members")
    @classmethod
    def _unique(cls, value):
        if value is not None and len(value) != len(set(value)):
            raise ValueError("entries must be unique")
        return value

    @field_validator("lead_hours")
    @classmethod
    def _leads(cls, value):
        if any(not 0 <= lead <= 384 for lead in value):
            raise ValueError("lead hours must be in [0, 384]")
        return value


class MemberValues(BaseModel):
    member: str
    values: dict[str, float]


class LeadForecast(BaseModel):
    lead_hours: int
    valid_time: datetime
    members: list[MemberValues]


class PointForecast(BaseModel):
    point: ForecastPoint
    forecasts: list[LeadForecast]


class ForecastResponse(BaseModel):
    source: Source
    cycle: datetime
    results: list[PointForecast]
    service: ServiceInfo = Field(default_factory=ServiceInfo)


def _load_subset(req: ForecastRequest) -> dict:
    filename = f"{req.source}/{req.cycle:%Y%m%d%H}.json"
    try:
        if settings.forecast_subset_dir:
            with (Path(settings.forecast_subset_dir) / filename).open("rb") as stream:
                raw = stream.read(settings.forecast_max_bytes + 1)
        elif settings.forecast_live_fetch and settings.forecast_subset_url:
            parts = urlsplit(settings.forecast_subset_url)
            if parts.scheme != "https" or not parts.netloc or parts.username or parts.query or parts.fragment:
                raise HTTPException(503, "forecast subset URL configuration is invalid")
            # Source/cycle are validated; the caller cannot supply a URL/path.
            with urlopen(settings.forecast_subset_url.rstrip("/") + "/" + filename,
                         timeout=settings.fetch_timeout_s) as stream:
                raw = stream.read(settings.forecast_max_bytes + 1)
        else:
            raise HTTPException(503, "forecast subset archive is not configured")
    except (OSError, URLError) as exc:
        raise HTTPException(503, "forecast subset is unavailable") from exc
    if len(raw) > settings.forecast_max_bytes:
        raise HTTPException(502, "forecast subset exceeds the byte limit")
    try:
        data = json.loads(raw)
        if data["version"] != 1 or data["source"] != req.source:
            raise ValueError("unsupported subset")
        cycle = datetime.fromisoformat(data["cycle"].replace("Z", "+00:00"))
        if cycle.tzinfo is None or cycle != req.cycle:
            raise ValueError("wrong cycle")
        lats, lons = (np.asarray(data[key], dtype=float) for key in ("lats", "lons"))
        for axis, low, high in [(lats, -90, 90), (lons, -180, 180)]:
            if (axis.ndim != 1 or len(axis) < 2 or not np.isfinite(axis).all()
                    or (np.diff(axis) <= 0).any() or axis[0] < low or axis[-1] > high):
                raise ValueError("invalid grid")
        leads, members = data["lead_hours"], data["members"]
        if (not leads or len(leads) > 64 or len(set(leads)) != len(leads)
                or any(type(h) is not int or not 0 <= h <= 384 for h in leads)
                or not members or len(members) > 31 or len(set(members)) != len(members)
                or any(not isinstance(m, str) or not m or len(m) > 64 for m in members)
                or (req.source == "gfs_fx" and members != ["control"])):
            raise ValueError("invalid lead/member axes")
        shape = (len(leads), len(members), len(lats), len(lons))
        arrays = {key: np.asarray(values, dtype=float) for key, values in data["variables"].items()}
        if not arrays or set(arrays) - set(Variable.__args__):
            raise ValueError("invalid variables")
        if sum(a.size for a in arrays.values()) > 100_000:
            raise ValueError("subset cell limit")
        if any(a.shape != shape or not np.isfinite(a).all() for a in arrays.values()):
            raise ValueError("invalid field shape/values")
        return {"lats": lats, "lons": lons, "leads": leads, "members": members, "arrays": arrays}
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
        raise HTTPException(502, "forecast subset is malformed or mismatches the requested cycle") from exc


def compute_forecast(req: ForecastRequest) -> ForecastResponse:
    data = _load_subset(req)
    members = req.members if req.members is not None else data["members"]
    if (set(req.lead_hours) - set(data["leads"]) or set(members) - set(data["members"])
            or set(req.variables) - set(data["arrays"])):
        raise HTTPException(422, "requested lead, member or variable is absent from the subset")
    if len(req.points) * len(req.lead_hours) * len(members) * len(req.variables) > 10_000:
        raise HTTPException(422, "forecast extraction exceeds the point-value limit")
    results = []
    for point in req.points:
        lon = normalize_lon(point.lon)
        if not (data["lats"][0] <= point.lat <= data["lats"][-1]
                and data["lons"][0] <= lon <= data["lons"][-1]):
            raise HTTPException(422, "point is outside the forecast subset")
        forecasts = []
        for lead in req.lead_hours:
            index = data["leads"].index(lead)
            values = []
            for member in members:
                mi = data["members"].index(member)
                fields = {var: bilinear(data["lats"], data["lons"],
                                        data["arrays"][var][index, mi], point.lat, lon).value
                          for var in req.variables}
                if not all(math.isfinite(value) for value in fields.values()):
                    raise HTTPException(502, "forecast interpolation is not finite")
                values.append(MemberValues(member=member, values=fields))
            try:
                valid_time = req.cycle + timedelta(hours=lead)
            except OverflowError as exc:
                raise HTTPException(422, "forecast valid time exceeds the supported range") from exc
            forecasts.append(LeadForecast(lead_hours=lead, valid_time=valid_time, members=values))
        results.append(PointForecast(point=point, forecasts=forecasts))
    return ForecastResponse(source=req.source, cycle=req.cycle, results=results)
