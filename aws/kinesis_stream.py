"""Kinesis replacement for local/local_stream.py's asyncio.Queue.

Same two methods the rest of the pipeline uses — `publish` and `consume` — so
poller and processor are unchanged. StreamRecord is imported from the local
module rather than redefined: it is the wire contract between poller and
processor, not a local-implementation detail.

PartitionKey is icao24, so every update for one aircraft lands on one shard and
is therefore processed in order. Ordering per aircraft is what matters — the
delta filter compares each state against the previous one for that icao24, and
out-of-order delivery would compare a new fix against a newer stored one and
suppress a real move.

The consumer here is a manual GetRecords loop. In phase 5 this file's consume()
disappears entirely: a Lambda event source mapping does the polling, checkpoints
for you, and hands batches to a handler. Everything about iterators and
throttling below is scaffolding for that, not the destination.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from typing import Any, Awaitable, Callable, Optional

import boto3
from botocore.exceptions import ClientError

from core.models import AircraftState
from local.local_stream import StreamRecord

logger = logging.getLogger(__name__)

STREAM_NAME = "aerofeed-aircraft-states"

# GetRecords is capped at 5 calls/second/shard. One call per shard per interval
# stays well inside that with room for several shards.
POLL_INTERVAL_S = 1.0

# Per GetRecords call. The service caps a response at 10k records or 10MB
# anyway; this bounds how much one iteration hands to the processor at once.
BATCH_LIMIT = 500

# Kinesis records are ~600 bytes here, so a full batch is well under the 5MB/s
# per-shard read limit. Backoff on throttling rather than hammering.
THROTTLE_BACKOFF_S = 2.0


class KinesisError(RuntimeError):
    """Stream unreachable or answering unusably."""


def encode(record: StreamRecord) -> bytes:
    """Serialise a record for the wire.

    None-valued fields are dropped: most of AircraftState is Optional and
    absent on any given aircraft, and at a few hundred records per poll the
    nulls are most of the payload.
    """
    return json.dumps(
        {
            "region_cell": record.region_cell,
            "state": {k: v for k, v in asdict(record.state).items() if v is not None},
        },
        separators=(",", ":"),
    ).encode()


def decode(data: bytes) -> Optional[StreamRecord]:
    """Parse a record off the wire, or None if it is unusable.

    Returns None rather than raising: one malformed record must not stall a
    shard, and Kinesis will hand it back on every retry until the iterator
    moves past it.
    """
    try:
        payload = json.loads(data)
        state = payload["state"]
        return StreamRecord(
            state=AircraftState(**state),
            region_cell=payload["region_cell"],
        )
    except (ValueError, TypeError, KeyError) as exc:
        logger.warning("dropping undecodable kinesis record: %s", exc)
        return None


class KinesisStream:
    """A Kinesis-backed record stream with the LocalStream interface."""

    def __init__(
        self,
        stream_name: str = STREAM_NAME,
        kinesis=None,
        poll_interval_s: float = POLL_INTERVAL_S,
        batch_limit: int = BATCH_LIMIT,
    ) -> None:
        self._stream = stream_name
        self._client = kinesis or boto3.client("kinesis")
        self._poll_interval = poll_interval_s
        self._batch_limit = batch_limit

    async def publish(self, record: StreamRecord) -> None:
        """Put one record on the stream.

        to_thread because boto3 is blocking and this is called from the poller's
        event loop, which also serves the WebSocket clients.

        ponytail: one PutRecord per aircraft, so a 700-aircraft cell is 700 API
        calls. PutRecords batches up to 500 at a time and is the obvious next
        step — it needs partial-failure handling (per-record error codes, retry
        only the failures), which is real code and not needed to validate the
        shape here.
        """
        try:
            await asyncio.to_thread(
                self._client.put_record,
                StreamName=self._stream,
                Data=encode(record),
                PartitionKey=record.state.icao24,
            )
        except ClientError as exc:
            raise KinesisError(f"put_record failed: {exc}") from exc

    def _shard_ids(self) -> list[str]:
        """Every shard currently in the stream."""
        shards: list[str] = []
        kwargs: dict[str, Any] = {"StreamName": self._stream}
        while True:
            response = self._client.list_shards(**kwargs)
            shards.extend(s["ShardId"] for s in response["Shards"])
            token = response.get("NextToken")
            if not token:
                return shards
            # NextToken and StreamName are mutually exclusive on this call.
            kwargs = {"NextToken": token}

    def _iterator(self, shard_id: str, start: str = "LATEST") -> str:
        """A shard iterator.

        LATEST by default: this consumer is a live tail, and TRIM_HORIZON would
        replay up to 24 hours of aircraft positions on every restart — all of
        them long stale.
        """
        return self._client.get_shard_iterator(
            StreamName=self._stream,
            ShardId=shard_id,
            ShardIteratorType=start,
        )["ShardIterator"]

    async def consume(
        self, processor: Callable[[StreamRecord], Awaitable[None]]
    ) -> None:
        """Pull records forever, handing each to `processor`.

        A processor failure is logged and the record dropped — one malformed
        state must not take down the consumer and stall every subscriber.
        Cancel the task to stop the loop.

        ponytail: no checkpointing and no resharding support. Iterators live in
        memory, so a restart resumes at LATEST and drops whatever arrived while
        down. Phase 5's event source mapping provides both for free; building a
        DynamoDB checkpoint table here would be work thrown away.
        """
        iterators = {shard: self._iterator(shard) for shard in await asyncio.to_thread(self._shard_ids)}
        logger.info("consuming %d shard(s) of %s", len(iterators), self._stream)

        while True:
            for shard_id, iterator in list(iterators.items()):
                if iterator is None:
                    continue
                try:
                    response = await asyncio.to_thread(
                        self._client.get_records, ShardIterator=iterator, Limit=self._batch_limit
                    )
                except ClientError as exc:
                    code = exc.response["Error"]["Code"]
                    if code == "ProvisionedThroughputExceededException":
                        logger.warning("shard %s throttled; backing off", shard_id)
                        await asyncio.sleep(THROTTLE_BACKOFF_S)
                        continue
                    if code == "ExpiredIteratorException":
                        # Iterators live 5 minutes. Re-open rather than dying.
                        logger.warning("iterator for %s expired; reopening", shard_id)
                        iterators[shard_id] = await asyncio.to_thread(self._iterator, shard_id)
                        continue
                    raise KinesisError(f"get_records failed: {exc}") from exc

                for raw in response["Records"]:
                    record = decode(raw["Data"])
                    if record is None:
                        continue
                    try:
                        await processor(record)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            "processor failed for %s in %s",
                            record.state.icao24, record.region_cell,
                        )

                # None means the shard is closed (split or merged). Drop it;
                # its children appear on the next list_shards.
                iterators[shard_id] = response.get("NextShardIterator")
                lag = response.get("MillisBehindLatest")
                if lag and lag > 10_000:
                    logger.warning("shard %s is %.0fs behind", shard_id, lag / 1000)

            await asyncio.sleep(self._poll_interval)


def stream_is_active(stream_name: str = STREAM_NAME, kinesis=None) -> bool:
    """True when the stream exists and is ACTIVE.

    Checked at startup so a missing stream fails with a sentence naming it,
    rather than an exception on the first published record.
    """
    client = kinesis or boto3.client("kinesis")
    try:
        summary = client.describe_stream_summary(StreamName=stream_name)
        return summary["StreamDescriptionSummary"]["StreamStatus"] == "ACTIVE"
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            return False
        raise
