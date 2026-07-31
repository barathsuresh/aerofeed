"""Snap a coordinate to the region cell containing it.

The pipeline polls OpenSky per cell, not per client, so a thousand subscribers
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
