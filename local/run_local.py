"""Entrypoint: wires the local pipeline together and runs it.

    python run_local.py

Then connect a client:

    websocat 'ws://127.0.0.1:8765/?lat=51.5&lon=-0.1'

Flow: scheduler -> poller -> stream -> processor -> websocket broadcast.
Nothing here touches AWS; every stand-in is swappable at exactly one wiring
line below.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Lets the file run as `python run_local.py`, `python local/run_local.py` or
# `python -m local.run_local` — otherwise only the last one can import `core`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from core.airplanes_live_client import AirplanesLiveClient  # noqa: E402
from local.local_scheduler import LocalScheduler  # noqa: E402
from local.local_stream import LocalStream  # noqa: E402
from local.local_ws_server import LocalWebSocketServer  # noqa: E402
from local.poller import Poller  # noqa: E402
from local.processor import Processor  # noqa: E402
from local.sqlite_store import SqliteConnectionStore, SqlitePositionStore, connect  # noqa: E402

logger = logging.getLogger("aerofeed")


async def main() -> None:
    """Start the server, scheduler and consumer, and run until interrupted."""
    db = connect(config.DB_PATH)
    position_store = SqlitePositionStore(db)
    connection_store = SqliteConnectionStore(db)

    # Sockets do not survive a restart, so rows from a previous run are dead.
    # Left behind, they would keep the poller running with nobody listening.
    connection_store.clear()

    stream = LocalStream()
    ws_server = LocalWebSocketServer(
        connection_store,
        default_lat=config.DEFAULT_LAT,
        default_lon=config.DEFAULT_LON,
        grid_size_degrees=config.GRID_SIZE_DEGREES,
        max_cells=config.MAX_CELLS_PER_CLIENT,
        position_store=position_store,
    )
    processor = Processor(position_store, broadcast=ws_server.broadcast)
    poller = Poller(
        AirplanesLiveClient(),
        connection_store,
        stream,
        grid_size_degrees=config.GRID_SIZE_DEGREES,
    )
    scheduler = LocalScheduler(poller, connection_store, config.POLL_INTERVAL_S)

    # Assigned rather than passed: the server needs the scheduler and the
    # scheduler needs the poller which needs the store, so the cycle has to be
    # closed after construction. Wakes the poller on every new subscription, so
    # sky the user just panned to is fetched now instead of at the next tick.
    ws_server.on_subscribe = scheduler.wake

    # db.close() must outlive the server: closing the socket server cancels the
    # live connection handlers, whose cleanup deregisters them from the store.
    # Closing the database first makes every one of those fail with
    # "Cannot operate on a closed database".
    try:
        async with ws_server.serve(config.WS_HOST, config.WS_PORT):
            logger.info("websocket listening on ws://%s:%d", config.WS_HOST, config.WS_PORT)
            logger.info(
                "default subscription point %.3f/%.3f; override with ?lat=&lon=",
                config.DEFAULT_LAT,
                config.DEFAULT_LON,
            )
            consumer = asyncio.create_task(stream.consume(processor.process), name="consumer")
            ticker = asyncio.create_task(scheduler.run(), name="scheduler")
            try:
                # Both run forever; if either dies unexpectedly, surface it
                # rather than sitting there looking healthy with half the
                # pipeline gone.
                await asyncio.gather(consumer, ticker)
            finally:
                for task in (consumer, ticker):
                    task.cancel()
                await asyncio.gather(consumer, ticker, return_exceptions=True)
    finally:
        db.close()


def cli() -> None:
    """Configure logging and run. The single entrypoint both launchers call."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("shutting down")


if __name__ == "__main__":
    cli()
