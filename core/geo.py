"""Snap a coordinate to the region cell containing it.

The pipeline polls upstream per cell, not per client, so a thousand subscribers
watching the same sky cost one upstream request.

Known tradeoff (documented, not fixed): snapping is a hard partition. Two points
a hundred metres apart across a cell boundary land in different cells, so a
client near an edge sees its own side only. Fixing it needs neighbour fan-out or
per-client boxes, both of which multiply upstream calls. Pinned by
tests/test_geo.py.
"""

from __future__ import annotations

import math

from .models import RegionCell

LAT_MIN, LAT_MAX = -90.0, 90.0
LON_MIN, LON_MAX = -180.0, 180.0

# One minute of latitude, by definition of the nautical mile.
NM_PER_DEGREE = 60.0

# Most cells one client may cover at once.
#
# The binding constraint is upstream, not local: airplanes.live allows 1
# request/second, and the poller issues one request per *distinct* cell across
# all clients. At a 60s cycle that is a hard ceiling of ~60 cells for the whole
# service, so a single client claiming 9 of them is a sixth of global capacity.
#
# 4 covers a 2x2 viewport — a comfortable regional view at zoom 6-7 — and lets
# ~15 clients watch entirely different parts of the world before the budget is
# gone. Raise it only alongside the rotation scheduling in
# docs/poll-scheduling.md, which is what removes the ceiling rather than
# rationing under it.
DEFAULT_MAX_CELLS = 4


def _format_edge(value: float | int) -> str:
    """Render a cell edge for a key: "40", not "40.0".

    Keys are persisted, so a formatting change would orphan live subscriptions.
    """
    return str(int(value)) if float(value).is_integer() else str(float(value))


def snap_to_grid(lat: float, lon: float, grid_size_degrees: float = 5) -> RegionCell:
    """Snap a coordinate to its fixed-size grid cell.

    Cells are named by their south-west corner: (42.3, -73.1) on a 5-degree grid
    gives key "40_-75".

    Args:
        lat: WGS-84 latitude, decimal degrees.
        lon: WGS-84 longitude, decimal degrees.
        grid_size_degrees: Cell edge length. Bigger cells mean fewer upstream
            polls but more irrelevant traffic per subscriber.

    Returns:
        The RegionCell containing the point, bbox clamped to legal WGS-84 range.

    Raises:
        ValueError: Non-positive grid size, or coordinates out of range.
            Coordinates come from a third-party API and from user
            subscriptions, so they are validated rather than trusted.
    """
    if grid_size_degrees <= 0:
        raise ValueError(f"grid_size_degrees must be positive, got {grid_size_degrees!r}")
    if not LAT_MIN <= lat <= LAT_MAX:
        raise ValueError(f"latitude out of range [-90, 90]: {lat!r}")
    if not LON_MIN <= lon <= LON_MAX:
        raise ValueError(f"longitude out of range [-180, 180]: {lon!r}")

    # floor(), not truncation: -73.1 must map to -75, not -70, or the grid gaps.
    lamin = math.floor(lat / grid_size_degrees) * grid_size_degrees
    lomin = math.floor(lon / grid_size_degrees) * grid_size_degrees

    # A point at lat == 90 floors into a cell whose far edge would be illegal.
    lamax = min(lamin + grid_size_degrees, LAT_MAX)
    lomax = min(lomin + grid_size_degrees, LON_MAX)

    return RegionCell(
        key=f"{_format_edge(lamin)}_{_format_edge(lomin)}",
        lamin=float(lamin),
        lomin=float(lomin),
        lamax=float(lamax),
        lomax=float(lomax),
    )


def cells_for_bounds(
    lamin: float,
    lomin: float,
    lamax: float,
    lomax: float,
    grid_size_degrees: float = 5,
    max_cells: int = DEFAULT_MAX_CELLS,
) -> list[RegionCell]:
    """Every cell overlapping a viewport, nearest the centre first.

    A zoomed-out map spans more than one cell, and a client subscribed to one
    cell sees one cell's worth of aircraft however far out it zooms — the rest
    of the sky is real, just never polled.

    Ordering is by distance from the viewport centre so that truncation drops
    the corners rather than an arbitrary slice: what remains is always a
    contiguous blob around where the user is actually looking.

    Args:
        lamin, lomin, lamax, lomax: Viewport edges in decimal degrees.
        grid_size_degrees: Must match the poller's grid.
        max_cells: Hard cap. Each cell is one upstream request per poll cycle
            and the poller paces them 1.1s apart, so an uncapped world view
            would take longer to poll than the interval between polls.

    Returns:
        Between 1 and `max_cells` cells. Never empty — a degenerate viewport
        still resolves to the cell containing it.

    Raises:
        ValueError: Non-positive grid size, max_cells below 1, or coordinates
            out of range.
    """
    if max_cells < 1:
        raise ValueError(f"max_cells must be at least 1, got {max_cells!r}")

    # Clamp before snapping: a world-view map reports edges past the poles, and
    # Leaflet happily reports longitudes outside +-180 once you scroll sideways.
    lamin, lamax = max(LAT_MIN, min(lamin, lamax)), min(LAT_MAX, max(lamin, lamax))
    lomin, lomax = max(LON_MIN, min(lomin, lomax)), min(LON_MAX, max(lomin, lomax))

    origin = snap_to_grid(lamin, lomin, grid_size_degrees)
    centre_lat = (lamin + lamax) / 2.0
    centre_lon = (lomin + lomax) / 2.0

    cells: dict[str, RegionCell] = {}
    lat = origin.lamin
    while lat <= lamax and lat < LAT_MAX:
        lon = origin.lomin
        while lon <= lomax and lon < LON_MAX:
            cell = snap_to_grid(lat, lon, grid_size_degrees)
            cells[cell.key] = cell
            lon += grid_size_degrees
        lat += grid_size_degrees

    def distance_from_centre(cell: RegionCell) -> float:
        cell_lat = (cell.lamin + cell.lamax) / 2.0
        cell_lon = (cell.lomin + cell.lomax) / 2.0
        return math.hypot(cell_lat - centre_lat, cell_lon - centre_lon)

    ordered = sorted(cells.values(), key=lambda c: (distance_from_centre(c), c.key))
    return ordered[:max_cells]


def cell_to_point_radius(cell: RegionCell) -> tuple[float, float, float]:
    """Convert a cell to the (lat, lon, radius_nm) circle that covers it.

    airplanes.live queries a circle, not a box, so the square cell has to be
    circumscribed. The circle is deliberately the smallest one that contains
    the whole cell rather than the largest one inside it: over-covering means a
    client occasionally sees an aircraft just outside its box, while
    under-covering means a corner of the map is silently always empty.

    Args:
        cell: The cell to cover.

    Returns:
        (centre latitude, centre longitude, radius in nautical miles).

    Note:
        Returns the true circumscribing radius; the client clamps it to the
        provider's 250 nm limit so this stays pure geometry. A 5-degree grid
        needs at most ~212 nm, so the clamp only bites if GRID_SIZE_DEGREES is
        raised past ~6 degrees — at which point cells genuinely stop being
        coverable in one request and would need splitting rather than clamping.
    """
    centre_lat = (cell.lamin + cell.lamax) / 2.0
    centre_lon = (cell.lomin + cell.lomax) / 2.0

    half_height_nm = (cell.lamax - cell.lamin) / 2.0 * NM_PER_DEGREE

    # A degree of longitude is widest at the equator, so scale by whichever
    # edge sits closer to it. Using the centre latitude would under-cover the
    # cell's equator-facing corners — exactly the silent data hole above.
    widest_lat = min(abs(cell.lamin), abs(cell.lamax))
    half_width_nm = (
        (cell.lomax - cell.lomin) / 2.0 * NM_PER_DEGREE * math.cos(math.radians(widest_lat))
    )

    return centre_lat, centre_lon, math.hypot(half_height_nm, half_width_nm)


def cell_from_key(key: str, grid_size_degrees: float = 5) -> RegionCell:
    """Rebuild a RegionCell from its key.

    The poller reads bare cell keys back out of the connection store and needs
    the bounding box to query with. Storing the box alongside every connection
    would duplicate what the key already encodes.

    Must be called with the same grid size that produced the key — a mismatch
    silently yields a different box, so the grid size lives in one config value.

    Raises:
        ValueError: If the key is not a well-formed "<lamin>_<lomin>" pair.
    """
    lat_text, _, lon_text = key.partition("_")
    try:
        # Round-trip through snap_to_grid so the box and the key are always
        # produced by the same code path and cannot drift apart.
        return snap_to_grid(float(lat_text), float(lon_text), grid_size_degrees)
    except ValueError as exc:
        raise ValueError(f"malformed region cell key: {key!r}") from exc
