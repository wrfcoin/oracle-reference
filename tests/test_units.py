import pytest

from app import units


def test_k_to_c_round_trip():
    assert units.k_to_c(273.15) == 0.0
    assert units.k_to_c(units.c_to_k(-4.4)) == pytest.approx(-4.4)


def test_pa_to_hpa():
    assert units.pa_to_hpa(99260.0) == pytest.approx(992.6)


def test_m_to_mm():
    assert units.m_to_mm(0.0025) == pytest.approx(2.5)


def test_wind_speed_from_components():
    assert units.wind_speed(3.0, 4.0) == pytest.approx(5.0)
    assert units.wind_speed(0.0, 0.0) == 0.0
    assert units.wind_speed(-3.0, 4.0) == pytest.approx(5.0)


def test_saturation_vapor_pressure_known_values():
    # Magnus formula: es(0 degC) = 6.112 hPa; es(20 degC) ~ 23.4 hPa.
    assert units.saturation_vapor_pressure_hpa(0.0) == pytest.approx(6.112)
    assert units.saturation_vapor_pressure_hpa(20.0) == pytest.approx(23.4, abs=0.2)


def test_rh_from_t_dewpoint():
    # Dewpoint == temperature -> saturation.
    assert units.rh_from_t_dewpoint(15.0, 15.0) == pytest.approx(100.0)
    # 20 degC with 10 degC dewpoint is ~52-53% RH.
    assert units.rh_from_t_dewpoint(20.0, 10.0) == pytest.approx(52.5, abs=1.0)
    # Clamped: dewpoint above temperature never exceeds 100.
    assert units.rh_from_t_dewpoint(10.0, 12.0) == 100.0
