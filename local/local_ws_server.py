"""Stand-in for API Gateway WebSocket: connect, subscribe, broadcast.

On connect a client is assigned exactly one region cell and recorded in the
ConnectionStore; on disconnect the record is removed. Broadcast pushes only to
sockets whose cell matches the record's, which is the same fan-out filter the
processor applies in production.

Two registries, on purpose: the ConnectionStore holds durable subscription
metadata (and is what the poller reads), while `_sockets` holds the live socket
objects, which cannot be serialised and do not survive a restart.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from core.geo import snap_to_grid
from core.models import Connection

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
    ) -> None:
        """Create the server.

        Args:
            connection_store: Anything satisfying `ConnectionStore`.
            default_lat: Latitude used when a client sends no override.
            default_lon: Longitude used when a client sends no override.
            grid_size_degrees: Must match the poller's grid, or clients
                subscribe to cells nothing is ever polled for.
        """
        self._store = connection_store
        self._default_lat = default_lat
        self._default_lon = default_lon
        self._grid = grid_size_degrees
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
            cell = snap_to_grid(lat, lon, self._grid)
        except ValueError as exc:
            # Out-of-range coordinates: tell the client why rather than dropping
            # the socket with no explanation.
            logger.warning("rejecting %s: %s", connection_id, exc)
            await websocket.close(code=1008, reason=str(exc))
            return

        self._store.put_connection(
            Connection(
                connection_id=connection_id,
                cell_key=cell.key,
                connected_at=time.time(),
            )
        )
        self._sockets[connection_id] = websocket
        logger.info(
            "connect %s from %s -> cell %s (%s, %.3f/%.3f)",
            connection_id, peer, cell.key, source, lat, lon,
        )

        # Tell the client what it actually got. Without this a subscriber has no
        # way to know which patch of sky it is watching.
        await websocket.send(
            json.dumps(
                {
                    "type": "subscribed",
                    "connection_id": connection_id,
                    "region_cell": cell.key,
                    "bbox": cell.bbox,
                }
            )
        )

        try:
            # No client->server protocol yet; this just parks until the socket
            # closes. Reading is what surfaces the disconnect.
            async for _ in websocket:
                pass
        except ConnectionClosed:
            pass
        finally:
            # finally, not after the loop: an abrupt drop or a server shutdown
            # must still deregister, or the poller keeps polling a dead cell.
            self._sockets.pop(connection_id, None)
            self._store.delete_connection(connection_id, cell.key)
            logger.info("disconnect %s (cell %s)", connection_id, cell.key)

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
