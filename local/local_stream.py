"""In-process stand-in for Kinesis: an asyncio.Queue with a consume loop.

Deliberately the thinnest thing that preserves the shape of the real system —
producer and consumer decoupled, records processed one at a time, backpressure
when the consumer falls behind. Swapping in Kinesis later replaces this file
and nothing else, because poller and processor only touch `publish` and
`consume`.

Not durable: a crash drops in-flight records. That is the correct tradeoff for
local dev and the main thing Kinesis buys you later.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from core.models import AircraftState

logger = logging.getLogger(__name__)

# Bounded so a stalled processor applies backpressure to the poller instead of
# growing the queue until the process dies. One poll of a busy cell is a few
# hundred aircraft, so this holds several polls' worth.
DEFAULT_MAX_QUEUE = 5000


@dataclass(frozen=True, slots=True)
class StreamRecord:
    """One aircraft state tagged with the cell it was polled for.

    The tag rides alongside the state rather than inside it: which cell a
    request came from is a routing fact, not a property of the aircraft, and
    the same aircraft can appear in two cells' polls at a boundary.
    """

    state: AircraftState
    region_cell: str


class LocalStream:
    """A single-partition record stream backed by an asyncio.Queue."""

    def __init__(self, max_queue: int = DEFAULT_MAX_QUEUE) -> None:
        self._queue: asyncio.Queue[StreamRecord] = asyncio.Queue(maxsize=max_queue)

    async def publish(self, record: StreamRecord) -> None:
        """Put a record on the stream, waiting if the consumer is behind."""
        await self._queue.put(record)

    def qsize(self) -> int:
        """Current depth. Useful as a lag signal when watching logs."""
        return self._queue.qsize()

    async def consume(
        self, processor: Callable[[StreamRecord], Awaitable[None]]
    ) -> None:
        """Pull records forever, handing each to `processor`.

        A processor failure is logged and the record dropped — one malformed
        state must not take down the consumer and stall every subscriber. Cancel
        the task to stop the loop.
        """
        while True:
            record = await self._queue.get()
            try:
                await processor(record)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "processor failed for %s in %s", record.state.icao24, record.region_cell
                )
            finally:
                self._queue.task_done()
