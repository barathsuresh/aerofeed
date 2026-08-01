"""Entrypoint: the same pipeline as run_local.py, on real AWS services.

    python run_aws.py

Identical flow — scheduler -> poller -> stream -> processor -> websocket — with
three substitutions and nothing else:

    SqlitePositionStore    -> DynamoPositionStore    (aircraft-positions)
    SqliteConnectionStore  -> DynamoConnectionStore  (ws-connections)
    LocalStream            -> KinesisStream          (aerofeed-aircraft-states)

Still a plain process, deliberately. Phase 5 wraps the poller and processor in
Lambda handlers and replaces the WebSocket server with API Gateway; this phase
exists to prove the AWS-specific behaviour — DynamoDB round-trips, TTL, GSI
queries, Kinesis partitioning and ordering — before any of that is layered on.

Resources are created out of band (AWS CLI for now, Terraform later), so this
checks they exist and exits with a usable message rather than failing on the
first poll.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from aws.dynamo_store import (  # noqa: E402
    CONNECTIONS_TABLE,
    POSITIONS_TABLE,
    DynamoConnectionStore,
    DynamoPositionStore,
    table_exists,
)
from aws.kinesis_stream import STREAM_NAME, KinesisStream, stream_is_active  # noqa: E402
from core.airplanes_live_client import AirplanesLiveClient  # noqa: E402
from local.local_scheduler import LocalScheduler  # noqa: E402
from local.local_ws_server import LocalWebSocketServer  # noqa: E402
from local.poller import Poller  # noqa: E402
from local.processor import Processor  # noqa: E402

logger = logging.getLogger("aerofeed")


def check_resources() -> list[str]:
    """Return a list of problems, empty when everything is present."""
    problems = []
    for table in (POSITIONS_TABLE, CONNECTIONS_TABLE):
        if not table_exists(table):
            problems.append(f"DynamoDB table {table!r} is missing or not ACTIVE")
    if not stream_is_active(STREAM_NAME):
        problems.append(f"Kinesis stream {STREAM_NAME!r} is missing or not ACTIVE")
    return problems


async def main() -> None:
    """Start the server, scheduler and consumer, and run until interrupted."""
    position_store = DynamoPositionStore(grid_size_degrees=config.GRID_SIZE_DEGREES)
    connection_store = DynamoConnectionStore()

    # Sockets do not survive a restart, so rows from a previous run are dead.
    # Left behind, they keep the poller fetching cells nobody is listening to —
    # and unlike SQLite, that costs money.
    connection_store.clear()

    stream = KinesisStream()
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
    ws_server.on_subscribe = scheduler.wake

    async with ws_server.serve(config.WS_HOST, config.WS_PORT):
        logger.info("websocket listening on ws://%s:%d", config.WS_HOST, config.WS_PORT)
        logger.info(
            "dynamodb=%s,%s kinesis=%s", POSITIONS_TABLE, CONNECTIONS_TABLE, STREAM_NAME
        )
        consumer = asyncio.create_task(stream.consume(processor.process), name="consumer")
        ticker = asyncio.create_task(scheduler.run(), name="scheduler")
        try:
            await asyncio.gather(consumer, ticker)
        finally:
            for task in (consumer, ticker):
                task.cancel()
            await asyncio.gather(consumer, ticker, return_exceptions=True)


def cli() -> None:
    """Configure logging, verify the AWS resources exist, and run."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # boto3 logs every request at INFO, which buries the pipeline's own output.
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)

    problems = check_resources()
    if problems:
        for problem in problems:
            logger.error("%s", problem)
        logger.error("create the resources first; see aws/README.md")
        raise SystemExit(1)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("shutting down")


if __name__ == "__main__":
    cli()
