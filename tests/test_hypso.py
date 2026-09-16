import pytest

from app import hypso


def test_correction_direction():
    # Station below grid elevation -> higher pressure at station.
    p = hypso.correct_pressure_to_station(
        p_grid_hpa=991.0, z_grid_m=212.0, z_station_m=186.0, t_grid_k=283.0
    )
    assert p > 991.0
    assert p == pytest.approx(994.1, abs=0.3)
    # Station above grid elevation -> lower pressure.
    p_up = hypso.correct_pressure_to_station(
        p_grid_hpa=991.0, z_grid_m=212.0, z_station_m=500.0, t_grid_k=283.0
    )
    assert p_up < 991.0


def test_correction_round_trip():
    p_down = hypso.correct_pressure_to_station(1000.0, 800.0, 200.0, 285.0)
    # Temperature at the lower end of the same lapse column.
    t_at_station = 285.0 + hypso.LAPSE * (800.0 - 200.0)
    p_back = hypso.correct_pressure_to_station(p_down, 200.0, 800.0, t_at_station)
    # Same layer-mean temperature both ways -> exact inverse.
    assert p_back == pytest.approx(1000.0, abs=1e-9)


def test_zero_dz_is_identity():
    assert hypso.correct_pressure_to_station(991.0, 300.0, 300.0, 280.0) == 991.0


def test_pressure_implied_elevation_standard_atmosphere():
    # Standard-atmosphere pressure at 430 m (Duluth-airport-like) is ~962.6 hPa.
    z = hypso.pressure_implied_elevation_m(962.6)
    assert z == pytest.approx(430.0, abs=5.0)
    # Denver-like: ~835 hPa implies ~1600 m.
    z_denver = hypso.pressure_implied_elevation_m(835.0)
    assert z_denver == pytest.approx(1600.0, abs=25.0)
    # Sea level.
    assert hypso.pressure_implied_elevation_m(hypso.P0_HPA) == pytest.approx(0.0, abs=0.1)


def test_pressure_implied_elevation_inverts_correction():
    # Build a synthetic ob: sea-level pressure corrected up to 430 m, then invert.
    p_at_430 = hypso.correct_pressure_to_station(
        hypso.P0_HPA, 0.0, 430.0, hypso.T0_K
    )
    z = hypso.pressure_implied_elevation_m(p_at_430, t_obs_k=hypso.T0_K - hypso.LAPSE * 430.0)
    # ~4 m residual from the exponential-vs-power-law model difference.
    assert z == pytest.approx(430.0, abs=10.0)


def test_reference_mslp_removes_synoptic_swing():
    # A 970 hPa ob during a deep low (reference MSLP 985) implies a much lower
    # elevation than the same ob judged against the standard atmosphere.
    z_with_ref = hypso.pressure_implied_elevation_m(970.0, p0_hpa=985.0)
    z_standard = hypso.pressure_implied_elevation_m(970.0)
    assert z_with_ref < z_standard
    assert z_with_ref == pytest.approx(130.0, abs=15.0)
