"""Consume stream records, filter by delta, fan out to matching subscribers.

The throttle that makes the whole thing viable: OpenSky re-reports every
aircraft every poll, and only the ones that actually moved reach a client.

On top of the delta filter sits a heartbeat. Without it, an aircraft that never
crosses a threshold — parked at a gate, taxiing at 10 m/s — is broadcast once
and then never again, and any client with a staleness sweep eventually drops it
even though it is still there and still being polled.
"""

from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable

from core.delta import has_meaningfully_changed

from .local_stream import StreamRecord

logger = logging.getLogger(__name__)

# Re-send an unchanged state after this long. Must sit comfortably under the
# frontend's 90s staleness cutoff so a stationary aircraft is refreshed before
# it is swept, with room for a missed poll.
HEARTBEAT_S = 60.0


class Processor:
    """Delta check, persist, broadcast."""

    def __init__(
        self,
        position_store,
        broadcast: Callable[[StreamRecord], Awaitable[int]],
        heartbeat_s: float = HEARTBEAT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create the processor.

        Args:
            position_store: Anything satisfying `PositionStore`.
            broadcast: Coroutine taking a record and returning a delivery count.
                Injected rather than imported so this module has no dependency
                on the transport — the Lambda version passes a different one.
            heartbeat_s: Maximum silence per aircraft before an unchanged state
                is re-sent anyway.
            clock: Monotonic time source. Injected so tests can advance it
                without sleeping.
        """
        self._store = position_store
        self._broadcast = broadcast
        self._heartbeat = heartbeat_s
        self._clock = clock
        # icao24 -> when we last broadcast it. ponytail: in-memory, so a restart
        # re-broadcasts everything once (harmless) and nothing is ever evicted
        # (a few dozen bytes per distinct aircraft). Move it into the position
        # store if this ever runs somewhere stateless, like Lambda.
        self._last_broadcast: dict[str, float] = {}

    async def process(self, record: StreamRecord) -> bool:
        """Handle one record.

        Returns:
            True if the record was broadcast, whether by change or heartbeat.
        """
        state = record.state
        previous = self._store.get_position(state.icao24)
        now = self._clock()

        changed = has_meaningfully_changed(previous, state)
        last_sent = self._last_broadcast.get(state.icao24)
        due_for_heartbeat = last_sent is None or (now - last_sent) >= self._heartbeat

        if not changed and not due_for_heartbeat:
            return False

        if changed:
            # Store before broadcasting: a send failure must not cause the same
            # state to be re-broadcast on every subsequent poll. Dropping one
            # update is cheaper than a stuck aircraft spamming every client.
            #
            # Deliberately NOT written on a heartbeat-only send. Overwriting the
            # baseline each heartbeat would let sub-threshold drift accumulate
            # from a moving reference point and never trip the delta check — an
            # aircraft creeping 0.009 degrees per poll would go unreported
            # forever. Keeping the original baseline means the drift eventually
            # adds up and fires normally.
            self._store.put_position(state)

        self._last_broadcast[state.icao24] = now

        delivered = await self._broadcast(record)
        logger.debug(
            "broadcast %s in %s to %d client(s)%s",
            state.icao24,
            record.region_cell,
            delivered,
            "" if changed else " (heartbeat)",
        )
        return True
