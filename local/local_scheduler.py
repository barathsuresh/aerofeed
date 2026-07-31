"""Stand-in for the EventBridge Rule and its enable/disable toggle.

EventBridge would be disabled outright when nobody is connected, so the poller
never runs and the account burns nothing. The local equivalent is this loop
checking the subscriber count each tick and skipping the poll when it is zero —
same behaviour, same zero upstream calls, no infrastructure.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class LocalScheduler:
    """Ticks the poller on an interval, but only while clients are connected."""

    def __init__(self, poller, connection_store, interval_s: float) -> None:
        """Create the scheduler.

        Args:
            poller: Object with an async `poll_once()`.
            connection_store: Anything satisfying `ConnectionStore`.
            interval_s: Seconds between ticks. Production runs at 60s to stay
                inside OpenSky's credit budget; local defaults to 15s so a
                change is visible in seconds rather than a minute. Safe here
                because local dev polls one or two cells at low volume.
        """
        self._poller = poller
        self._store = connection_store
        self._interval = interval_s

    async def run(self) -> None:
        """Loop until cancelled."""
        logger.info("scheduler started (interval %.0fs)", self._interval)
        while True:
            try:
                if self._store.count_connections() > 0:
                    await self._poller.poll_once()
                else:
                    logger.debug("no subscribers; poll skipped")
            except asyncio.CancelledError:
                raise
            except Exception:
                # A scheduler that dies on a transient error takes the whole
                # pipeline with it and is silent about it. Log and tick again.
                logger.exception("poll tick failed; continuing")

            await asyncio.sleep(self._interval)
