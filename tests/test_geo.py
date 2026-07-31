"""Tests for grid snapping, including the documented boundary tradeoff."""

from __future__ import annotations

import pytest

from core.geo import cell_from_key, snap_to_grid


def test_snaps_to_south_west_corner():
    cell = snap_to_grid(42.3, -73.1)
    assert cell.key == "40_-75"
    assert cell.bbox == (40.0, -75.0, 45.0, -70.0)


def test_negative_coordinates_floor_downward():
    # Truncation toward zero would give "-70" here and leave a hole in the grid.
    assert snap_to_grid(-33.9, -73.1).key == "-35_-75"


def test_point_on_a_cell_edge_belongs_to_the_cell_above():
    assert snap_to_grid(40.0, -75.0).key == "40_-75"


def test_origin():
    assert snap_to_grid(0.0, 0.0).key == "0_0"


def test_custom_grid_size():
    cell = snap_to_grid(42.3, -73.1, grid_size_degrees=10)
    assert cell.key == "40_-80"
    assert cell.bbox == (40.0, -80.0, 50.0, -70.0)


def test_fractional_grid_size_keeps_decimals_only_where_needed():
    # 43.0 -> 42.5 needs the decimal; -73.1 -> -75.0 is integral and drops it.
    assert snap_to_grid(43.0, -73.1, grid_size_degrees=2.5).key == "42.5_-75"


def test_extreme_coordinates_clamp_the_bbox():
    # Cell at the pole would otherwise claim lamax=95 and be rejected upstream.
    cell = snap_to_grid(90.0, 180.0)
    assert cell.lamax == 90.0
    assert cell.lomax == 180.0


@pytest.mark.parametrize(
    "lat, lon",
    [(91.0, 0.0), (-90.1, 0.0), (0.0, 180.5), (0.0, -181.0)],
)
def test_out_of_range_coordinates_raise(lat, lon):
    with pytest.raises(ValueError):
        snap_to_grid(lat, lon)


def test_non_positive_grid_size_raises():
    with pytest.raises(ValueError):
        snap_to_grid(0.0, 0.0, grid_size_degrees=0)


@pytest.mark.parametrize(
    "lat, lon",
    [(42.3, -73.1), (-33.9, -73.1), (0.0, 0.0), (51.5, 0.1), (90.0, 180.0)],
)
def test_cell_from_key_round_trips(lat, lon):
    # The poller rebuilds cells from bare keys; a lossy round trip would send
    # it querying a different patch of sky than the client subscribed to.
    cell = snap_to_grid(lat, lon)
    assert cell_from_key(cell.key) == cell


@pytest.mark.parametrize("key", ["", "nonsense", "40", "40_", "_-75", "a_b"])
def test_cell_from_key_rejects_malformed_keys(key):
    with pytest.raises(ValueError):
        cell_from_key(key)


def test_neighbouring_points_across_a_boundary_land_in_different_cells():
    """Pins the known tradeoff documented in core/geo.py.

    These two points are ~2 km apart but straddle the 40-degree line, so they
    get different cells and never see each other's traffic. Hard partition is
    the accepted cost of one upstream poll per cell; this test exists to make
    the behaviour deliberate, not to be "fixed".
    """
    below = snap_to_grid(39.999, -74.5)
    above = snap_to_grid(40.001, -74.5)
    assert below.key == "35_-75"
    assert above.key == "40_-75"
    assert below.key != above.key
