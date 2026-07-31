"""Poll OpenSky once per active region cell and publish to the stream.

Cell-driven, not client-driven: a hundred clients watching the same sky produce
one upstream request. Cells with no subscribers are never polled at all.
"""

from __future__ import annotations

import asyncio
import logging

from core.geo import cell_from_key
from core.opensky_client import OpenSkyClient, OpenSkyError

from .local_stream import LocalStream, StreamRecord

logger = logging.getLogger(__name__)


class Poller:
    """Fetches states for every cell that currently has subscribers."""

    def __init__(
        self,
        client: OpenSkyClient,
        connection_store,
        stream: LocalStream,
        grid_size_degrees: float = 5,
    ) -> None:
        self._client = client
        self._store = connection_store
        self._stream = stream
        self._grid = grid_size_degrees

    async def poll_once(self) -> int:
        """Poll every active cell and publish the results.

        Returns:
            Total records published across all cells.
        """
        cells = list(self._store.list_active_cells())
        if not cells:
            logger.debug("no active cells; skipping poll")
            return 0

        published = 0
        for cell_key in cells:
            published += await self._poll_cell(cell_key)
        return published

    async def _poll_cell(self, cell_key: str) -> int:
        """Poll one cell. Failures are logged and skipped, never raised.

        One bad cell must not abort the others or kill the scheduler loop; the
        next tick retries it in 15 seconds anyway.
        """
        try:
            cell = cell_from_key(cell_key, self._grid)
        except ValueError:
            logger.exception("skipping unparseable cell key %r", cell_key)
            return 0

        try:
            # to_thread because opensky_client uses blocking `requests`. Without
            # it a slow upstream (read timeout is 30s) would freeze the
            # WebSocket server and every other task on the loop.
            states = await asyncio.to_thread(self._client.get_states, *cell.bbox)
        except OpenSkyError as exc:
            logger.warning("poll failed for cell %s: %s", cell_key, exc)
            return 0

        for state in states:
            await self._stream.publish(StreamRecord(state=state, region_cell=cell_key))

        logger.info("polled cell %s -> %d states", cell_key, len(states))
        return len(states)
