"""Advisory mirror of core4 9ef92a235 weather/reference.rs, never reward policy."""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Literal

from app.bands import BANDS_VERSION, band

Tier = Literal["good", "degraded", "outlier"]
FoldRule = Literal["worst", "second_worst"]
EXPECTED_BANDS_VERSION = 3
FOLD_RULE: FoldRule = "second_worst"
FOLD_RULE_BY_BANDS_VERSION = {1: "worst", 2: "second_worst", 3: "second_worst"}
if BANDS_VERSION != EXPECTED_BANDS_VERSION or FOLD_RULE != FOLD_RULE_BY_BANDS_VERSION[BANDS_VERSION]:
    raise RuntimeError("reference bands version/fold rule differs from deployed core4")

_RANK = {"good": 0, "degraded": 1, "outlier": 2}


def variable_tier(source: str, variable: str, residual: float) -> Tier | None:
    b = band(source, variable)
    if b is None:
        return None
    if not math.isfinite(residual):
        return "outlier"
    magnitude = abs(residual)
    if magnitude <= b["good_max"]:
        return "good"
    return "degraded" if magnitude <= b["marginal_max"] else "outlier"


def fold_variable_tiers(tiers: list[Tier], rule: FoldRule | None = None) -> Tier:
    rule = FOLD_RULE if rule is None else rule
    if rule not in ("worst", "second_worst"):
        raise ValueError("unknown reference fold rule")
    if not tiers:
        return "good"
    ordered = sorted(tiers, key=_RANK.__getitem__)
    if rule == "worst":
        return ordered[-1]
    outliers = tiers.count("outlier")
    if outliers >= 2 or (outliers == 1 and len(tiers) == 1):
        return "outlier"
    kept = ordered[:-1] if outliers == 1 or len(tiers) >= 3 else ordered
    base = kept[-1]
    return "degraded" if outliers == 1 else base


def tier_residuals(residuals: Iterable[Mapping], *, gross_guard_active: bool = False) -> Tier:
    scored = []
    gross = False
    for r in residuals:
        tier = variable_tier(r["source"], r["variable"], r["residual"])
        if tier is None:
            continue
        scored.append(tier)
        b = band(r["source"], r["variable"])
        gross |= (tier == "outlier" and math.isfinite(r["residual"])
                  and b.get("gross_max") is not None and abs(r["residual"]) > b["gross_max"])
    if gross_guard_active and len(scored) >= 2 and scored.count("outlier") == 1 and gross:
        return "outlier"
    return fold_variable_tiers(scored)


def gross_guard_active_at(height: int, activation_height: int | None) -> bool:
    """Inclusive feature-schedule activation; None means unscheduled, as core4."""
    for value in (height, activation_height):
        if value is not None and (type(value) is not int or not 0 <= value <= 2**64 - 1):
            raise ValueError("chain heights must be unsigned 64-bit integers")
    return activation_height is not None and height >= activation_height
