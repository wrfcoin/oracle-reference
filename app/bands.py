"""Residual bands, version 1 — embedded copy of the consensus-side fixture.

This is DATA, not verdicts: the same public band table that core4 ships at
``shared-protocol/fixtures/reference_bands.json`` (merged in core4#2054, commit
225e281f4). The service uses it only to express residuals as dimensionless
band ratios (|residual| / marginal_max) so mixed-unit residuals become
comparable; the residual→tier mapping itself is consensus-adjacent and lives
in core4 Rust, never here.

``tests/fixtures/reference_bands_v1.json`` is a byte-for-byte copy of the
core4 fixture; ``tests/test_bands.py`` asserts this module matches it so any
upstream recalibration (a ``version`` bump) fails loudly instead of drifting.
"""

from __future__ import annotations

BANDS_VERSION = 1

# variable -> canonical unit, for documentation and validation.
CANONICAL_VARIABLES: dict[str, str] = {
    "temperature_c": "degC",
    "pressure_hpa": "hPa",
    "humidity_pct": "%",
    "wind_speed_ms": "m/s",
    "rainfall_mm": "mm",
}

# source -> variable -> {good_max, marginal_max}, canonical units.
BANDS: dict[str, dict[str, dict[str, float]]] = {
    "hrrr": {
        "temperature_c": {"good_max": 2.0, "marginal_max": 5.0},
        "pressure_hpa": {"good_max": 2.5, "marginal_max": 6.0},
        "humidity_pct": {"good_max": 10.0, "marginal_max": 25.0},
        "wind_speed_ms": {"good_max": 3.0, "marginal_max": 7.0},
        "rainfall_mm": {"good_max": 2.0, "marginal_max": 10.0},
    },
    "hrrr_fx": {
        "temperature_c": {"good_max": 2.5, "marginal_max": 6.0},
        "pressure_hpa": {"good_max": 3.0, "marginal_max": 7.0},
        "humidity_pct": {"good_max": 12.0, "marginal_max": 30.0},
        "wind_speed_ms": {"good_max": 3.5, "marginal_max": 8.0},
        "rainfall_mm": {"good_max": 3.0, "marginal_max": 12.0},
    },
    "gfs": {
        "temperature_c": {"good_max": 3.0, "marginal_max": 7.0},
        "pressure_hpa": {"good_max": 3.5, "marginal_max": 8.0},
        "humidity_pct": {"good_max": 15.0, "marginal_max": 35.0},
        "wind_speed_ms": {"good_max": 4.0, "marginal_max": 9.0},
        "rainfall_mm": {"good_max": 5.0, "marginal_max": 15.0},
    },
    "gfs_fx": {
        "temperature_c": {"good_max": 3.5, "marginal_max": 8.0},
        "pressure_hpa": {"good_max": 4.0, "marginal_max": 9.0},
        "humidity_pct": {"good_max": 18.0, "marginal_max": 40.0},
        "wind_speed_ms": {"good_max": 4.5, "marginal_max": 10.0},
        "rainfall_mm": {"good_max": 6.0, "marginal_max": 18.0},
    },
    "mrms": {
        "rainfall_mm": {"good_max": 1.5, "marginal_max": 8.0},
    },
    "era5": {
        "temperature_c": {"good_max": 2.5, "marginal_max": 6.0},
        "pressure_hpa": {"good_max": 2.5, "marginal_max": 6.0},
        "humidity_pct": {"good_max": 12.0, "marginal_max": 30.0},
        "wind_speed_ms": {"good_max": 3.5, "marginal_max": 8.0},
    },
}


def band(source: str, variable: str) -> dict[str, float] | None:
    """Band for a (source, variable) pair, or None if the pair has no band.

    A missing band means the pair is skipped, mirroring core4's
    "no gating without reference data" rule.
    """
    return BANDS.get(source, {}).get(variable)


def band_ratio(source: str, variable: str, residual: float) -> float | None:
    """|residual| / marginal_max, or None when the pair has no band."""
    b = band(source, variable)
    if b is None:
        return None
    return abs(residual) / b["marginal_max"]


def variables_for(source: str) -> frozenset[str]:
    return frozenset(BANDS.get(source, {}))


def as_fixture_dict() -> dict:
    """Shape-compatible with core4's reference_bands.json (drift-alarm test)."""
    return {"version": BANDS_VERSION, "sources": BANDS}
