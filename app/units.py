"""Unit conversions to project canonical units (°C, hPa, m/s, mm, %RH).

Canonical variable names and units match core4
``shared-protocol/src/weather/reference.rs``: temperature_c, pressure_hpa,
humidity_pct, wind_speed_ms, rainfall_mm.
"""

from __future__ import annotations

import math

K_OFFSET = 273.15


def k_to_c(kelvin: float) -> float:
    return kelvin - K_OFFSET


def c_to_k(celsius: float) -> float:
    return celsius + K_OFFSET


def pa_to_hpa(pa: float) -> float:
    return pa / 100.0


def m_to_mm(metres: float) -> float:
    """Precip depth m → mm (ERA5/some GFS products report tp in metres)."""
    return metres * 1000.0


def wind_speed(u_ms: float, v_ms: float) -> float:
    """Speed from components. Interpolate u and v separately, THEN call this —
    interpolating speed directly biases low near direction shear."""
    return math.hypot(u_ms, v_ms)


def saturation_vapor_pressure_hpa(t_c: float) -> float:
    """Magnus formula (WMO-recommended coefficients), over water."""
    return 6.112 * math.exp(17.62 * t_c / (243.12 + t_c))


def rh_from_t_dewpoint(t_c: float, td_c: float) -> float:
    """Relative humidity (%) from temperature and dewpoint, clamped [0, 100]."""
    rh = 100.0 * saturation_vapor_pressure_hpa(td_c) / saturation_vapor_pressure_hpa(t_c)
    return max(0.0, min(100.0, rh))
