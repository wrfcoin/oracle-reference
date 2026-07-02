import numpy as np
import pytest

from app.interp import bilinear, haversine_km, normalize_lon


def linear_field(lats, lons):
    """f(lat, lon) = 3*lat + 2*lon + 1 — reproduced exactly by bilinear."""
    return 3.0 * lats + 2.0 * lons + 1.0


def test_normalize_lon():
    assert normalize_lon(268.0) == pytest.approx(-92.0)
    assert normalize_lon(-92.0) == pytest.approx(-92.0)
    assert normalize_lon(180.0) == pytest.approx(-180.0)
    assert normalize_lon(359.75) == pytest.approx(-0.25)


def test_haversine_known_distance():
    # One degree of latitude is ~111.2 km.
    assert haversine_km(46.0, -92.0, 47.0, -92.0) == pytest.approx(111.2, abs=0.5)
    assert haversine_km(46.8, -92.1, 46.8, -92.1) == 0.0


def test_regular_grid_exact_on_linear_field():
    lats = np.array([46.0, 46.25, 46.5, 46.75, 47.0])
    lons = np.array([-93.0, -92.75, -92.5, -92.25, -92.0])
    lon2d, lat2d = np.meshgrid(lons, lats)
    values = linear_field(lat2d, lon2d)
    r = bilinear(lats, lons, values, 46.61, -92.37)
    assert r.value == pytest.approx(3 * 46.61 + 2 * -92.37 + 1, abs=1e-9)
    assert 0.0 < r.nearest_distance_km < 20.0


def test_regular_grid_at_node_returns_node_value():
    lats = np.array([46.0, 46.5, 47.0])
    lons = np.array([-93.0, -92.5, -92.0])
    values = np.arange(9, dtype=float).reshape(3, 3)
    r = bilinear(lats, lons, values, 46.5, -92.5)
    assert r.value == pytest.approx(values[1, 1])
    assert r.nearest_distance_km == pytest.approx(0.0, abs=1e-6)


def test_regular_grid_edge_clamps_instead_of_extrapolating():
    lats = np.array([46.0, 46.5, 47.0])
    lons = np.array([-93.0, -92.5, -92.0])
    values = np.ones((3, 3))
    values[0, 0] = 5.0
    r = bilinear(lats, lons, values, 45.0, -94.0)  # outside the tile corner
    assert r.value == pytest.approx(5.0)


def test_regular_grid_rejects_descending_coords():
    lats = np.array([47.0, 46.5, 46.0])
    lons = np.array([-93.0, -92.5, -92.0])
    with pytest.raises(ValueError):
        bilinear(lats, lons, np.zeros((3, 3)), 46.5, -92.5)


def test_curvilinear_exact_on_linear_field():
    # A rotated/sheared (Lambert-like) grid: 2-D coordinate arrays.
    jj, ii = np.meshgrid(np.arange(6), np.arange(6), indexing="ij")
    lat2d = 46.0 + 0.03 * jj + 0.005 * ii
    lon2d = -93.0 + 0.04 * ii - 0.006 * jj
    values = linear_field(lat2d, lon2d)
    lat, lon = 46.071, -92.883  # strictly inside the tile
    r = bilinear(lat2d, lon2d, values, lat, lon)
    assert r.value == pytest.approx(3 * lat + 2 * lon + 1, abs=1e-6)


def test_curvilinear_at_node_returns_node_value():
    jj, ii = np.meshgrid(np.arange(4), np.arange(4), indexing="ij")
    lat2d = 46.0 + 0.03 * jj
    lon2d = -93.0 + 0.04 * ii
    values = (jj * 10 + ii).astype(float)
    r = bilinear(lat2d, lon2d, values, float(lat2d[2, 1]), float(lon2d[2, 1]))
    assert r.value == pytest.approx(21.0)
    assert r.nearest_distance_km == pytest.approx(0.0, abs=1e-6)


def test_curvilinear_shape_mismatch_rejected():
    with pytest.raises(ValueError):
        bilinear(np.zeros((3, 3)), np.zeros((3, 4)), np.zeros((3, 3)), 46.0, -92.0)
