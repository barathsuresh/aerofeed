"""Stand-in for API Gateway WebSocket: connect, subscribe, broadcast.

On connect a client is assigned exactly one region cell and recorded in the
ConnectionStore; on disconnect the record is removed. Broadcast pushes only to
sockets whose cell matches the record's, which is the same fan-out filter the
processor applies in production.

A client moves its subscription by sending {"type":"subscribe", bounds:[...]}
(or a bare lat/lon), which is what makes panning and zooming work: the cells
are otherwise fixed at connect time and the poller only ever fetches cells
someone is subscribed to. A viewport spanning several cells subscribes to all
of them, capped at `max_cells` — each cell is an upstream request paced 1.1s
from the last, so coverage trades directly against how long a poll cycle takes.

Two registries, on purpose: the ConnectionStore holds durable subscription
metadata (and is what the poller reads), while `_sockets` holds the live socket
objects, which cannot be serialised and do not survive a restart.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from core.geo import DEFAULT_MAX_CELLS, snap_to_grid
from core.models import Connection
from core.subscription import (
    SubscriptionError,
    apply_subscription,
    is_subscribe,
    resolve_cells,
    subscribed_frame,
)

from .local_stream import StreamRecord

logger = logging.getLogger(__name__)


class LocalWebSocketServer:
    """WebSocket endpoint plus a cell-filtered broadcast."""

    def __init__(
        self,
        connection_store,
        default_lat: float,
        default_lon: float,
        grid_size_degrees: float = 5,
        max_cells: int = DEFAULT_MAX_CELLS,
        on_subscribe: Optional[Callable[[], None]] = None,
        position_store=None,
    ) -> None:
        """Create the server.

        Args:
            connection_store: Anything satisfying `ConnectionStore`.
            default_lat: Latitude used when a client sends no override.
            default_lon: Longitude used when a client sends no override.
            grid_size_degrees: Must match the poller's grid, or clients
                subscribe to cells nothing is ever polled for.
            max_cells: Most cells one connection may cover. Each is an upstream
                request paced 1.1s from the last, so this bounds how long a
                poll cycle takes as much as it bounds the coverage.
            on_subscribe: Called after a subscription changes. The scheduler
                uses it to poll immediately instead of leaving a client staring
                at empty sky until the next tick.
            position_store: Optional store used to snapshot newly-covered cells.
        """
        self._store = connection_store
        self._positions = position_store
        self._default_lat = default_lat
        self._default_lon = default_lon
        self._grid = grid_size_degrees
        self._max_cells = max_cells
        # Public: run_local assigns this after the scheduler exists.
        self.on_subscribe = on_subscribe
        # connection_id -> live socket. Mirrors the store, minus durability.
        self._sockets: Dict[str, ServerConnection] = {}

    def _resolve_location(self, websocket: ServerConnection) -> Tuple[float, float, str]:
        """Work out which point on the globe a client is subscribing to.

        Production would geolocate the source IP. Loopback has no geolocation,
        so `?lat=&lon=` overrides for local testing and a configured default
        covers a client that passes neither.

        Returns:
            (lat, lon, source) where source names how it was resolved, for logs.
        """
        query = parse_qs(urlparse(websocket.request.path).query)
        lat_param = query.get("lat", [None])[0]
        lon_param = query.get("lon", [None])[0]

        if lat_param is not None and lon_param is not None:
            try:
                return float(lat_param), float(lon_param), "query override"
            except ValueError:
                # Bad input from a client is not a crash; fall through to the
                # default so a typo'd URL still gets a working subscription.
                logger.warning("ignoring non-numeric lat/lon: %r, %r", lat_param, lon_param)

        # The IP is read but not resolved locally — swap in a GeoIP lookup here
        # when this runs somewhere with real client addresses.
        return self._default_lat, self._default_lon, "default"

    async def handler(self, websocket: ServerConnection) -> None:
        """Serve one client for the life of its connection."""
        connection_id = str(websocket.id)
        peer = websocket.remote_address[0] if websocket.remote_address else "unknown"

        try:
            lat, lon, source = self._resolve_location(websocket)
            initial = [snap_to_grid(lat, lon, self._grid)]
        except ValueError as exc:
            # Out-of-range coordinates: tell the client why rather than dropping
            # the socket with no explanation.
            logger.warning("rejecting %s: %s", connection_id, exc)
            await websocket.close(code=1008, reason=str(exc))
            return

        # One cell to begin with; the client widens this to its viewport as
        # soon as its map has laid out and reported a size.
        cells = self._apply_subscription(connection_id, set(), initial)
        self._sockets[connection_id] = websocket
        logger.info(
            "connect %s from %s -> cell %s (%s, %.3f/%.3f)",
            connection_id, peer, initial[0].key, source, lat, lon,
        )

        # Tell the client what it actually got. Without this a subscriber has no
        # way to know which patch of sky it is watching.
        await self._send_subscribed(websocket, connection_id, initial)
        await self._send_snapshot(websocket, initial)

        try:
            async for raw in websocket:
                # The only client->server message: "I am looking here now."
                # Without it a subscription is fixed at connect time, so
                # panning shows empty sky and zooming out shows one cell's
                # worth of aircraft however far out you go.
                cells = await self._handle_message(websocket, connection_id, cells, raw)
        except ConnectionClosed:
            pass
        finally:
            # finally, not after the loop: an abrupt drop or a server shutdown
            # must still deregister, or the poller keeps polling dead cells.
            self._sockets.pop(connection_id, None)
            for key in cells:
                self._store.delete_connection(connection_id, key)
            logger.info("disconnect %s (cells %s)", connection_id, ",".join(sorted(cells)))

    def _apply_subscription(
        self, connection_id: str, current: set[str], wanted: list
    ) -> set[str]:
        """Move this connection's subscription to exactly `wanted`.

        Thin wrapper over core.subscription so the deployed Lambda and this
        server cannot drift apart. `current` comes from the handler coroutine's
        own state here; the Lambda reads it back from the store.
        """
        return apply_subscription(
            self._store, connection_id, current, wanted, time.time()
        )

    def _resolve_cells(self, message: dict) -> list:
        """Cells this subscribe message asks for. See core.subscription."""
        return resolve_cells(message, self._grid, self._max_cells)

    async def _handle_message(
        self, websocket: ServerConnection, connection_id: str, cells: set[str], raw: str
    ) -> set[str]:
        """Apply one client message and return the cells now subscribed to.

        Every failure path returns the *current* cells unchanged: a client
        sending nonsense should keep the aircraft it already has, not lose them
        or its socket.
        """
        try:
            message = json.loads(raw)
        except ValueError:
            logger.debug("ignoring unparseable message from %s", connection_id)
            return cells

        if not is_subscribe(message):
            return cells

        try:
            wanted = self._resolve_cells(message)
        except (SubscriptionError, ValueError) as exc:
            logger.warning("ignoring bad subscribe from %s: %s", connection_id, exc)
            return cells

        if {cell.key for cell in wanted} == cells:
            # Moving within the same coverage is the common case — the client
            # cannot know where the boundaries are, so it re-sends on every
            # map move and the server is what makes that cheap.
            return cells

        updated = self._apply_subscription(connection_id, cells, wanted)
        # Wake the poller now. Otherwise the sky the user just panned to stays
        # blank until the next scheduled tick, which is the whole 15s.
        if self.on_subscribe is not None:
            self.on_subscribe()
        logger.info(
            "resubscribe %s: %d -> %d cells (%s)",
            connection_id, len(cells), len(updated), ",".join(sorted(updated)),
        )
        await self._send_subscribed(websocket, connection_id, wanted)
        await self._send_snapshot(websocket, [cell for cell in wanted if cell.key not in cells])
        return updated

    async def _send_subscribed(self, websocket, connection_id: str, cells: list) -> None:
        """Tell the client exactly which sky it is now receiving."""
        await websocket.send(
            json.dumps(subscribed_frame(connection_id, cells, self._max_cells))
        )

    async def _send_snapshot(self, websocket, new_cells: list) -> None:
        """Replay stored aircraft for newly-covered cells, best effort."""
        if self._positions is None:
            return

        for cell in new_cells:
            for state in self._positions.list_positions_in_cell(cell.key):
                if not state.has_position:
                    continue
                await websocket.send(
                    json.dumps(
                        {
                            "type": "aircraft",
                            "region_cell": cell.key,
                            "state": asdict(state),
                            "snapshot": True,
                        }
                    )
                )

    async def broadcast(self, record: StreamRecord) -> int:
        """Push a record to every live client subscribed to its cell.

        Returns:
            Number of clients the record was delivered to.
        """
        subscribers = list(self._store.list_connections_by_cell(record.region_cell))
        if not subscribers:
            return 0

        message = json.dumps(
            {
                "type": "aircraft",
                "region_cell": record.region_cell,
                "state": asdict(record.state),
            }
        )

        delivered = 0
        for subscriber in subscribers:
            socket = self._sockets.get(subscriber.connection_id)
            if socket is None:
                # Store row with no live socket: a stale record from a crashed
                # run. Reap it so it stops inflating the poll set.
                self._store.delete_connection(subscriber.connection_id, record.region_cell)
                continue
            try:
                await socket.send(message)
                delivered += 1
            except ConnectionClosed:
                # Closed between the store read and the send. The handler's
                # finally block does the cleanup; nothing to do here.
                logger.debug("send to %s failed: closed", subscriber.connection_id)
        return delivered

    def serve(self, host: str, port: int):
        """Return the `serve` context manager for this handler."""
        return serve(self.handler, host, port)
