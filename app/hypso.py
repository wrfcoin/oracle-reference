"""Hypsometric relations — both directions.

(a) Grid→station surface-pressure correction: model orography rarely matches
    the station elevation; on sloped terrain this is the single largest source
    of false pressure residuals, so /v1/reference corrects grid surface
    pressure to the station elevation before differencing.

(b) Pressure-implied elevation: inverting the barometric formula against a
    station's observed pressure series is the strongest GPS-spoof signal a
    weather series carries (used by /v1/location-plausibility).

Standard-atmosphere constants; formulas assume the constant-lapse-rate lower
troposphere, which is accurate to a few metres over the |dz| < ~2 km range
these corrections operate in.
"""

from __future__ import annotations

G = 9.80665  # m s^-2
R_D = 287.05  # J kg^-1 K^-1, dry air
LAPSE = 0.0065  # K m^-1, standard lapse rate
T0_K = 288.15  # K, standard sea-level temperature
P0_HPA = 1013.25  # hPa, standard sea-level pressure
_EXPONENT = R_D * LAPSE / G  # 0.1902632...


def correct_pressure_to_station(
    p_grid_hpa: float,
    z_grid_m: float,
    z_station_m: float,
    t_grid_k: float,
) -> float:
    """Surface pressure at grid elevation → pressure at station elevation.

    Uses the hypsometric equation with the mean layer temperature estimated
    from the grid 2 m temperature and the standard lapse rate.
    """
    dz = z_station_m - z_grid_m
    # Temperature decreases with height: layer mean is T_grid - LAPSE*dz/2.
    t_layer = t_grid_k - LAPSE * dz / 2.0
    import math

    return p_grid_hpa * math.exp(-G * dz / (R_D * t_layer))


def pressure_implied_elevation_m(
    p_obs_hpa: float,
    t_obs_k: float | None = None,
    p0_hpa: float = P0_HPA,
) -> float:
    """Elevation implied by an observed station pressure.

    ``p0_hpa`` should be the reference mean-sea-level pressure at the claimed
    location and time when available (removes synoptic swings); falls back to
    the standard atmosphere otherwise.

    ``t_obs_k`` is the temperature observed AT the station. The constant-lapse
    solution needs the sea-level temperature of the column, so we solve the
    small fixed point T_sl = t_obs + LAPSE * z; with no temperature the
    standard sea-level temperature is used directly.
    """
    frac = 1.0 - (p_obs_hpa / p0_hpa) ** _EXPONENT
    if t_obs_k is None:
        return (T0_K / LAPSE) * frac
    z = 0.0
    for _ in range(3):
        z = ((t_obs_k + LAPSE * z) / LAPSE) * frac
    return z
