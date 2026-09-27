"""Embedded bands v3 from deployed core4 9ef92a235.

The independent byte-copy fixture and golden vectors pin this advisory mirror.
Per-variable gross_max is optional; it is not a global multiplier. Existing
band ratios and attestation confidence semantics remain unchanged.
"""

from __future__ import annotations

BANDS_VERSION = 3

# variable -> canonical unit, for documentation and validation.
CANONICAL_VARIABLES: dict[str, str] = {
    "temperature_c": "degC",
    "pressure_hpa": "hPa",
    "humidity_pct": "%",
    "wind_speed_ms": "m/s",
    "rainfall_mm": "mm",
}

# source -> variable -> {good_max, marginal_max}, canonical units.
BANDS: dict[str, dict[str, dict[str, float]]] = {'hrrr': {'temperature_c': {'good_max': 2.0, 'marginal_max': 5.0},
          'pressure_hpa': {'good_max': 2.5, 'marginal_max': 6.0},
          'humidity_pct': {'good_max': 10.0, 'marginal_max': 25.0},
          'wind_speed_ms': {'good_max': 3.0, 'marginal_max': 7.0},
          'rainfall_mm': {'good_max': 2.0, 'marginal_max': 10.0}},
 'hrrr_fx': {'temperature_c': {'good_max': 2.5, 'marginal_max': 6.0},
             'pressure_hpa': {'good_max': 3.0, 'marginal_max': 7.0},
             'humidity_pct': {'good_max': 12.0, 'marginal_max': 30.0},
             'wind_speed_ms': {'good_max': 3.5, 'marginal_max': 8.0},
             'rainfall_mm': {'good_max': 3.0, 'marginal_max': 12.0}},
 'gfs': {'temperature_c': {'good_max': 3.0, 'marginal_max': 7.0},
         'pressure_hpa': {'good_max': 3.5, 'marginal_max': 8.0},
         'humidity_pct': {'good_max': 15.0, 'marginal_max': 35.0},
         'wind_speed_ms': {'good_max': 4.0, 'marginal_max': 9.0},
         'rainfall_mm': {'good_max': 5.0, 'marginal_max': 15.0}},
 'gfs_fx': {'temperature_c': {'good_max': 3.5, 'marginal_max': 8.0},
            'pressure_hpa': {'good_max': 4.0, 'marginal_max': 9.0},
            'humidity_pct': {'good_max': 18.0, 'marginal_max': 40.0},
            'wind_speed_ms': {'good_max': 4.5, 'marginal_max': 10.0},
            'rainfall_mm': {'good_max': 6.0, 'marginal_max': 18.0}},
 'mrms': {'rainfall_mm': {'good_max': 1.5, 'marginal_max': 8.0}},
 'nws_station': {'temperature_c': {'good_max': 3.0, 'marginal_max': 6.0, 'gross_max': 9.0},
                 'pressure_hpa': {'good_max': 8.0, 'marginal_max': 15.0},
                 'humidity_pct': {'good_max': 15.0, 'marginal_max': 30.0, 'gross_max': 45.0},
                 'wind_speed_ms': {'good_max': 4.0, 'marginal_max': 9.0, 'gross_max': 13.5}},
 'era5': {'temperature_c': {'good_max': 2.5, 'marginal_max': 6.0},
          'pressure_hpa': {'good_max': 2.5, 'marginal_max': 6.0},
          'humidity_pct': {'good_max': 12.0, 'marginal_max': 30.0},
          'wind_speed_ms': {'good_max': 3.5, 'marginal_max': 8.0}}}


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
