"""Which cells a client is subscribed to, and how that set changes.

Lifted out of local/local_ws_server.py so the same logic serves both transports.
The local server holds a long-lived coroutine per client and tracks the current
cell set in memory; a Lambda behind API Gateway is stateless and reads it back
from the store. Only that one input differs, so it is a parameter — everything
else is identical, and a divergence between local and deployed behaviour here
would be invisible until someone panned a map in production.

No transport, no vendor SDK. The store is duck-typed to the ConnectionStore
protocol so this stays testable with a dict.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from .geo import DEFAULT_MAX_CELLS, cells_for_bounds, snap_to_grid
from .models import Connection, RegionCell

logger = logging.getLogger(__name__)

SUBSCRIBE = "subscribe"


class SubscriptionError(ValueError):
    """The message could not be read as a subscription request.

    Callers treat this as "ignore and keep the existing subscription", never as
    a reason to drop the connection — a client sending nonsense should keep the
    aircraft it already has.
    """


def is_subscribe(message: Any) -> bool:
    """True when `message` is a subscription request."""
    return isinstance(message, dict) and message.get("type") == SUBSCRIBE


def resolve_cells(
    message: dict,
    grid_size_degrees: float = 5,
    max_cells: int = DEFAULT_MAX_CELLS,
) -> list[RegionCell]:
    """Work out which cells a subscribe message is asking for.

    Accepts a viewport (`bounds`) or a bare point (`lat`/`lon`). The point form
    stays supported because it is all a client needs before its map has laid
    out and reported a size.

    Args:
        message: The parsed subscribe message.
        grid_size_degrees: Must match the poller's grid.
        max_cells: Coverage cap. Each cell is one upstream request.

    Returns:
        The cells to subscribe to, nearest the viewport centre first.

    Raises:
        SubscriptionError: The message carries neither a usable viewport nor a
            usable point.
    """
    bounds = message.get("bounds")
    if bounds is not None:
        try:
            lamin, lomin, lamax, lomax = (float(value) for value in bounds)
        except (TypeError, ValueError) as exc:
            raise SubscriptionError(f"unusable bounds: {bounds!r}") from exc
        return cells_for_bounds(lamin, lomin, lamax, lomax, grid_size_degrees, max_cells)

    try:
        return [snap_to_grid(float(message["lat"]), float(message["lon"]), grid_size_degrees)]
    except (KeyError, TypeError, ValueError) as exc:
        raise SubscriptionError(f"unusable lat/lon: {message!r}") from exc


def apply_subscription(
    store,
    connection_id: str,
    current: Iterable[str],
    wanted: list[RegionCell],
    connected_at: float,
) -> set[str]:
    """Move a connection's subscription to exactly `wanted`.

    Args:
        store: Anything satisfying ConnectionStore.
        connection_id: The subscriber.
        current: Cell keys it holds now. In-memory for the local server, read
            back from the store for a Lambda.
        wanted: Cells it should hold.
        connected_at: Timestamp recorded on new rows.

    Returns:
        The cell keys now subscribed to.
    """
    current = set(current)
    wanted_keys = {cell.key for cell in wanted}

    # Add before removing, so a poll landing mid-swap never sees this client
    # unsubscribed from sky it is still looking at.
    for cell in wanted:
        if cell.key not in current:
            store.put_connection(
                Connection(
                    connection_id=connection_id,
                    cell_key=cell.key,
                    connected_at=connected_at,
                )
            )
    for key in current - wanted_keys:
        store.delete_connection(connection_id, key)

    return wanted_keys


def subscribed_frame(
    connection_id: str,
    cells: list[RegionCell],
    max_cells: int,
) -> dict:
    """The message telling a client exactly which sky it now receives.

    `truncated` matters: at a wide zoom the cap bites and the client is seeing
    part of its viewport. Saying so is the difference between "the sky is empty
    there" and "nobody asked about that sky".
    """
    return {
        "type": "subscribed",
        "connection_id": connection_id,
        # Kept for clients that only understand a single cell.
        "region_cell": cells[0].key,
        "bbox": list(cells[0].bbox),
        "cells": [{"key": cell.key, "bbox": list(cell.bbox)} for cell in cells],
        "truncated": len(cells) >= max_cells,
        "max_cells": max_cells,
    }
