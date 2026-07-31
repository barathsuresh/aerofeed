"""Poll airplanes.live once per active region cell and publish to the stream.

Cell-driven, not client-driven: a hundred clients watching the same sky produce
one upstream request. Cells with no subscribers are never polled at all.

The provider allows 1 request/second and the client deliberately does not
enforce it — a single call has nothing to pace against. This module does,
because it is the thing that knows how many calls a cycle contains.
"""

from __future__ import annotations

import asyncio
import logging

from core.airplanes_live_client import AirplanesLiveClient, AirplanesLiveError
from core.geo import cell_from_key, cell_to_point_radius

from .local_stream import LocalStream, StreamRecord

logger = logging.getLogger(__name__)

# Provider limit is 1 req/s. The extra 0.1s absorbs scheduling jitter — pacing
# exactly at the limit puts every request within a rounding error of a 429.
INTER_CALL_DELAY_S = 1.1


class Poller:
    """Fetches states for every cell that currently has subscribers."""

    def __init__(
        self,
        client: AirplanesLiveClient,
        connection_store,
        stream: LocalStream,
        grid_size_degrees: float = 5,
        inter_call_delay_s: float = INTER_CALL_DELAY_S,
    ) -> None:
        self._client = client
        self._store = connection_store
        self._stream = stream
        self._grid = grid_size_degrees
        self._delay = inter_call_delay_s

    async def poll_once(self) -> int:
        """Poll every active cell and publish the results.

        Cells are polled in sequence with INTER_CALL_DELAY_S between calls, so
        a cycle over N cells takes at least (N-1) * 1.1s. With the default 15s
        interval that is fine up to ~13 cells; past that, ticks would overlap
        and the scheduler's own guard is what to look at.

        Returns:
            Total records published across all cells.
        """
        cells = list(self._store.list_active_cells())
        if not cells:
            logger.debug("no active cells; skipping poll")
            return 0

        published = 0
        for index, cell_key in enumerate(cells):
            # Between calls, not after the last one, and not before the first:
            # a single-cell cycle should not pay for a rate limit it can't hit.
            # asyncio.sleep, not time.sleep — this coroutine shares its loop
            # with the WebSocket server, which blocking would freeze.
            if index:
                await asyncio.sleep(self._delay)
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

        # ponytail: the circle circumscribes the square cell, so corners pull in
        # aircraft just outside it and neighbouring cells overlap slightly. That
        # over-delivers rather than under-delivers, which is the safe direction.
        # Filter against cell.bbox here if the duplication ever costs anything.
        lat, lon, radius_nm = cell_to_point_radius(cell)

        try:
            # to_thread because the client uses blocking `requests`. Without it
            # a slow upstream (read timeout is 30s) would freeze the WebSocket
            # server and every other task on the loop.
            states = await asyncio.to_thread(self._client.get_states, lat, lon, radius_nm)
        except AirplanesLiveError as exc:
            logger.warning("poll failed for cell %s: %s", cell_key, exc)
            return 0

        for state in states:
            await self._stream.publish(StreamRecord(state=state, region_cell=cell_key))

        logger.info("polled cell %s -> %d states", cell_key, len(states))
        return len(states)
