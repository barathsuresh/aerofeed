"""Kinesis event source mapping -> delta filter -> push to WebSocket clients.

Thin adapter. The behaviour is Processor.process() from local/processor.py:
compare against the last stored state, skip anything that has not meaningfully
changed, store and broadcast the rest, with a heartbeat so parked aircraft do
not age off a client's map.

The one thing this file adds that the local pipeline had no equivalent of is
GoneException handling. API Gateway returns 410 for a connection that closed
without a $disconnect reaching us — a tab killed, a lid shut, a network drop.
That row would otherwise keep its cell in the poll set forever, meaning real
upstream calls and real Kinesis writes for a subscriber that no longer exists.
A 410 therefore reaps the row on the spot.

Failures are reported per record, not raised. The event source mapping is
configured with ReportBatchItemFailures, so returning the failing sequence
numbers lets the mapping retry and eventually DLQ just those records instead of
replaying an entire batch because one aircraft was malformed. Raising here
would fail the whole batch and defeat that.

Test locally:
    from lambdas.processor_handler import handler
    handler({"Records": [{"kinesis": {"data": <base64 of encode(record)>}}]}, None)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import asdict
from functools import lru_cache

from botocore.exceptions import ClientError

from aws.kinesis_stream import decode
from local.local_stream import StreamRecord
from local.processor import Processor

from ._common import get_connection_store, get_management_api, get_position_store

logger = logging.getLogger(__name__)

# API Gateway returns this when the socket is already closed. Matched by name
# and by status because the modelled exception surfaces as either depending on
# whether the service model was loaded.
GONE = ("GoneException", "410")


def encode_message(record: StreamRecord) -> bytes:
    """Serialise a record into the frame the frontend already parses.

    Same shape LocalWebSocketServer.broadcast sends, so frontend/app.js needs
    no change when the transport becomes API Gateway.
    """
    return json.dumps(
        {
            "type": "aircraft",
            "region_cell": record.region_cell,
            "state": asdict(record.state),
        }
    ).encode()


class BroadcastToApiGateway:
    """Pushes a record to every live client subscribed to its cell.

    Same shape as LocalWebSocketServer.broadcast — takes a StreamRecord,
    returns a delivery count — so Processor is untouched.
    """

    def __init__(self, store, api=None) -> None:
        self._store = store
        self._api = api

    @property
    def api(self):
        """Resolved lazily so importing this module needs no endpoint set."""
        if self._api is None:
            self._api = get_management_api()
        return self._api

    async def __call__(self, record: StreamRecord) -> int:
        """Deliver to the record's cell. Returns how many clients got it."""
        subscribers = list(self._store.list_connections_by_cell(record.region_cell))
        if not subscribers:
            return 0

        message = encode_message(record)
        delivered = 0
        for subscriber in subscribers:
            # to_thread: botocore is blocking, and Processor.process is async.
            if await asyncio.to_thread(self._post, subscriber.connection_id, message):
                delivered += 1
        return delivered

    def _post(self, connection_id: str, message: bytes) -> bool:
        """Post to one connection. True if delivered.

        A 410 reaps every row for that connection — every row, not just this
        cell's, because a client at a wide zoom holds one per covered cell and
        leaving the rest keeps the poller fetching sky for a dead socket.

        Any other ClientError propagates. An auth failure or a throttle is not
        something to paper over by deleting a live subscriber's registration.
        """
        try:
            self.api.post_to_connection(ConnectionId=connection_id, Data=message)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in GONE:
                raise
            removed = self._store.delete_all_for_connection(connection_id)
            logger.info("connection %s gone; reaped %d row(s)", connection_id, removed)
            return False


@lru_cache(maxsize=1)
def get_processor() -> Processor:
    """Built once per container; deferred so importing needs no AWS config."""
    return Processor(
        get_position_store(),
        broadcast=BroadcastToApiGateway(get_connection_store()),
    )


def handler(event, context):
    """Process one Kinesis batch, reporting individual failures.

    Args:
        event: Event source mapping payload —
            {"Records": [{"kinesis": {"data": <base64>, "sequenceNumber": ...}}]}
        context: Lambda context. Unused.

    Returns:
        {"batchItemFailures": [{"itemIdentifier": <sequenceNumber>}, ...]}

        The shape the event source mapping expects when FunctionResponseTypes
        includes ReportBatchItemFailures. An empty list means the whole batch
        succeeded and the mapping checkpoints past all of it.

    Note:
        Kinesis is an ordered log, so this does not work the way SQS's
        per-message version does: the mapping takes the **lowest** reported
        sequence number and retries from there, replaying everything after it.
        Reporting every failure rather than just the first is still correct —
        AWS picks the minimum — and it makes the log show the true blast
        radius rather than only the earliest symptom.

        Because of that replay, records after a failure will be delivered
        twice. That is safe here: reprocessing runs the same delta check
        against the stored state and simply finds nothing changed.
    """
    records = event.get("Records", [])

    async def run() -> tuple[int, int, list[str]]:
        processed = sent = 0
        failures: list[str] = []
        for raw in records:
            sequence_number = raw["kinesis"].get("sequenceNumber")
            try:
                data = base64.b64decode(raw["kinesis"]["data"])
                record = decode(data)
                if record is None:
                    # Undecodable is not the same as failed. A retry cannot fix
                    # malformed bytes, so reporting it would replay the batch
                    # until the record aged out of the stream. Drop it and move
                    # on — this is the one failure mode retrying cannot help.
                    logger.warning("skipping undecodable record %s", sequence_number)
                    continue
                if await get_processor().process(record):
                    sent += 1
                processed += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                # Caught, not propagated: raising fails the entire batch, which
                # is precisely what partial batch reporting exists to avoid.
                logger.exception("record %s failed", sequence_number)
                failures.append(sequence_number)
        return processed, sent, failures

    processed, sent, failures = asyncio.run(run())
    logger.info(
        "processed %d record(s), broadcast %d, failed %d", processed, sent, len(failures)
    )
    return {
        "batchItemFailures": [{"itemIdentifier": s} for s in failures if s]
    }
