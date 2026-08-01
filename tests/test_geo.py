"""Tests for grid snapping, including the documented boundary tradeoff."""

from __future__ import annotations

import math

import pytest

from core.airplanes_live_client import MAX_RADIUS_NM
from core.geo import (
    DEFAULT_MAX_CELLS,
    cell_from_key,
    cell_to_point_radius,
    cells_for_bounds,
    snap_to_grid,
)


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


# --- point + radius conversion -----------------------------------------------


def test_cell_to_point_radius_centres_on_the_cell():
    lat, lon, _ = cell_to_point_radius(snap_to_grid(42.3, -73.1))
    assert (lat, lon) == (42.5, -72.5)


@pytest.mark.parametrize(
    "lat,lon",
    [(42.3, -73.1), (0.5, 0.5), (-33.9, 151.2), (67.0, 20.0), (-80.0, -60.0)],
)
def test_circle_covers_every_corner_of_its_cell(lat, lon):
    """Under-covering is a silent data hole; over-covering is just extra traffic.

    Checked away from the equator in both hemispheres because the longitude
    scaling is what makes this non-trivial: a degree of longitude shrinks with
    latitude, so the widest part of a cell is its equator-facing edge.
    """
    cell = snap_to_grid(lat, lon)
    centre_lat, centre_lon, radius_nm = cell_to_point_radius(cell)

    corners = [
        (cell.lamin, cell.lomin),
        (cell.lamin, cell.lomax),
        (cell.lamax, cell.lomin),
        (cell.lamax, cell.lomax),
    ]
    for corner_lat, corner_lon in corners:
        north_nm = (corner_lat - centre_lat) * 60.0
        # Scale by whichever of the two latitudes is nearer the equator, where
        # a degree of longitude is widest — the conservative reading.
        widest = min(abs(corner_lat), abs(centre_lat))
        east_nm = (corner_lon - centre_lon) * 60.0 * math.cos(math.radians(widest))
        assert math.hypot(north_nm, east_nm) <= radius_nm + 1e-9


def test_default_grid_fits_inside_the_provider_radius_limit():
    """The 5-degree grid must be coverable in one request, everywhere.

    The equator is the worst case: longitude degrees are at their widest, so a
    cell there needs the largest circle. If this ever fails, cells need
    splitting across several requests rather than a bigger radius.
    """
    _, _, radius_nm = cell_to_point_radius(snap_to_grid(0.1, 0.1))
    assert radius_nm <= MAX_RADIUS_NM
    assert radius_nm == pytest.approx(212.1, abs=0.5)


def test_radius_shrinks_toward_the_poles():
    equator = cell_to_point_radius(snap_to_grid(2.5, 2.5))[2]
    high = cell_to_point_radius(snap_to_grid(67.5, 2.5))[2]
    assert high < equator


# --- multi-cell viewport coverage --------------------------------------------


def test_a_viewport_inside_one_cell_gives_one_cell():
    cells = cells_for_bounds(41.0, -74.0, 42.0, -73.0)
    assert [c.key for c in cells] == ["40_-75"]


def test_a_wide_viewport_gives_every_cell_it_touches():
    """The zoomed-out bug: one cell of aircraft in a viewport spanning nine.

    Cap lifted deliberately — this is about coverage, not rationing.
    """
    cells = cells_for_bounds(38.0, -76.0, 47.0, -68.0, max_cells=99)
    assert {c.key for c in cells} == {"35_-80", "35_-75", "35_-70",
                                      "40_-80", "40_-75", "40_-70",
                                      "45_-80", "45_-75", "45_-70"}


def test_every_point_in_the_viewport_lands_in_a_returned_cell():
    """Coverage, stated as the property that actually matters: no gaps."""
    lamin, lomin, lamax, lomax = 38.0, -76.0, 47.0, -68.0
    keys = {c.key for c in cells_for_bounds(lamin, lomin, lamax, lomax, max_cells=99)}

    for lat in (lamin, (lamin + lamax) / 2, lamax - 0.001):
        for lon in (lomin, (lomin + lomax) / 2, lomax - 0.001):
            assert snap_to_grid(lat, lon).key in keys


def test_cells_are_ordered_nearest_the_centre_first():
    """So truncation drops the corners, leaving a blob around the viewport
    centre rather than an arbitrary slice of it."""
    cells = cells_for_bounds(38.0, -76.0, 47.0, -68.0, max_cells=99)
    assert cells[0].key == "40_-75"  # the cell containing the centre


def test_the_cap_is_enforced_and_keeps_the_centre():
    wide = cells_for_bounds(-60.0, -170.0, 60.0, 170.0, max_cells=9)
    assert len(wide) == 9
    # Centre of that viewport is 0/0, so its cell must survive truncation.
    assert "0_0" in {c.key for c in wide}


def test_a_world_view_does_not_explode():
    """Leaflet reports edges past the poles at low zoom; that must clamp, not
    produce hundreds of cells or an out-of-range error."""
    cells = cells_for_bounds(-95.0, -200.0, 95.0, 200.0, max_cells=9)
    assert len(cells) == 9
    assert all(-90.0 <= c.lamin <= 90.0 and -180.0 <= c.lomin <= 180.0 for c in cells)


def test_a_degenerate_viewport_still_gives_the_cell_it_is_in():
    """A map that has not laid out yet reports zero-size bounds."""
    cells = cells_for_bounds(42.3, -73.1, 42.3, -73.1)
    assert [c.key for c in cells] == ["40_-75"]


def test_inverted_bounds_are_normalised_not_rejected():
    assert cells_for_bounds(47.0, -68.0, 38.0, -76.0, max_cells=99) == \
           cells_for_bounds(38.0, -76.0, 47.0, -68.0, max_cells=99)


@pytest.mark.parametrize("max_cells", [0, -1])
def test_a_nonsense_cap_is_rejected(max_cells):
    with pytest.raises(ValueError, match="max_cells"):
        cells_for_bounds(38.0, -76.0, 47.0, -68.0, max_cells=max_cells)


def test_the_default_cap_fits_inside_the_poll_interval():
    """A full-cap client's cells must poll in well under one interval.

    If the cap ever exceeds this, poll cycles overlap and the feed falls behind
    rather than covering more sky.
    """
    from local.poller import INTER_CALL_DELAY_S
    assert (DEFAULT_MAX_CELLS - 1) * INTER_CALL_DELAY_S < 15.0


def test_the_cap_leaves_room_for_several_independent_clients():
    """The real constraint is upstream, not local: 1 request/second means ~60
    distinct cells per 60s cycle for the WHOLE service. The cap is what stops
    one zoomed-out client eating a large share of that."""
    upstream_budget_per_cycle = 60
    assert upstream_budget_per_cycle // DEFAULT_MAX_CELLS >= 15
