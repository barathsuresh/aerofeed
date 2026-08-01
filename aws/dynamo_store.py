"""DynamoDB implementations of the storage protocols in core/storage_interface.

Same contracts as local/sqlite_store.py, different engine. Nothing above this
module knows which one it is talking to — that is the point of the protocols.

Two tables:

  aircraft-positions  PK icao24                       TTL expires_at
  ws-connections      PK connection_id, SK region_cell TTL expires_at
                      GSI region_cell-index (region_cell, connection_id)

The connections table is keyed on the pair, not on connection_id alone: one
client covers up to `max_cells` region cells at a wide zoom, which is one row
per cell. A bare connection_id key would hold a single cell and quietly undo
multi-cell coverage. The GSI provides the reverse lookup — "who is watching
this cell" — which runs on every poll and must never be a table scan.

TTL is a garbage collector, never a correctness mechanism: AWS only commits to
deleting expired items within a few days. Every read path here is already
correct with expired items present, so lateness costs nothing.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, fields
from decimal import Decimal
from typing import Any, Iterable, Optional

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from core.geo import snap_to_grid
from core.models import AircraftState, Connection

logger = logging.getLogger(__name__)

POSITIONS_TABLE = "aircraft-positions"
CONNECTIONS_TABLE = "ws-connections"
REGION_CELL_INDEX = "region_cell-index"

# A position older than this is worthless — the aircraft has moved on. Expiry is
# not load-bearing: a stale item read back simply looks "changed" to the delta
# filter, which is the desired outcome anyway.
POSITION_TTL_S = 3600

# Matches API Gateway's 2h maximum WebSocket connection duration, so a row
# cannot outlive the connection it describes by more than the TTL's own lag.
# The real reaping mechanism is the explicit delete on disconnect; this only
# catches connections that died without one.
CONNECTION_TTL_S = 7200

# Field names AircraftState will accept. Items written by an older build can
# carry keys that no longer exist, so filtering on read means a stale table
# degrades instead of raising TypeError on every poll.
_STATE_FIELDS = {f.name for f in fields(AircraftState)}


def _to_dynamo(value: Any) -> Any:
    """Convert a Python value to something DynamoDB accepts.

    Floats are the whole reason this exists: DynamoDB has no float type and
    boto3 refuses them outright rather than rounding silently. Decimal(str(x))
    goes through the shortest repr, so 10972.8 stays 10972.8 instead of
    acquiring the binary-float tail that Decimal(float) would preserve.
    """
    if isinstance(value, float):
        return Decimal(str(value))
    return value


def _from_dynamo(value: Any) -> Any:
    """Convert a DynamoDB value back to a plain Python one."""
    if isinstance(value, Decimal):
        # AircraftState's numeric fields are float/int; Decimal would leak into
        # arithmetic in delta.py and compare oddly against float thresholds.
        return int(value) if value % 1 == 0 else float(value)
    return value


class DynamoPositionStore:
    """Last-known state per aircraft, satisfying `PositionStore`."""

    def __init__(
        self,
        table_name: str = POSITIONS_TABLE,
        dynamodb=None,
        ttl_s: int = POSITION_TTL_S,
        grid_size_degrees: float = 5,
        clock=time.time,
    ) -> None:
        """Create the store.

        Args:
            table_name: Positions table.
            dynamodb: Optional boto3 DynamoDB *resource*, injected for tests or
                to share a session. Created from the default session otherwise.
            ttl_s: Seconds until an item becomes eligible for deletion.
            grid_size_degrees: Used to derive the region_cell attribute. Must
                match the poller's grid or the attribute names a cell nothing
                polls.
            clock: Wall-clock source. Wall, not monotonic: TTL is an absolute
                unix timestamp that DynamoDB compares against its own clock.
        """
        self._table = (dynamodb or boto3.resource("dynamodb")).Table(table_name)
        self._ttl_s = ttl_s
        self._grid = grid_size_degrees
        self._clock = clock

    def _region_cell(self, state: AircraftState) -> Optional[str]:
        """Cell this aircraft sits in, or None when it has no position.

        Derived rather than passed in: put_position's signature takes only a
        state, and deriving keeps the attribute correct by construction instead
        of trusting whichever poll happened to write the row.
        """
        if not state.has_position:
            return None
        try:
            return snap_to_grid(state.latitude, state.longitude, self._grid).key
        except ValueError:
            # Coordinates out of range. The state is still worth storing; it
            # just cannot be attributed to a cell.
            logger.warning("un-cellable position for %s", state.icao24)
            return None

    def get_position(self, icao24: str) -> Optional[AircraftState]:
        """Last stored state, or None on miss.

        Eventually-consistent read on purpose: this feeds the delta filter, and
        a one-poll-stale baseline at worst re-broadcasts an aircraft once.
        Strong reads would double the cost for no behavioural gain.
        """
        response = self._table.get_item(Key={"icao24": icao24})
        item = response.get("Item")
        if item is None:
            return None

        payload = item.get("state")
        if not isinstance(payload, dict):
            logger.warning("position item for %s has no usable state", icao24)
            return None

        return AircraftState(
            **{k: _from_dynamo(v) for k, v in payload.items() if k in _STATE_FIELDS}
        )

    def list_positions_in_cell(self, cell_key: str, limit: int = 2000) -> list[AircraftState]:
        """Every stored aircraft currently attributed to `cell_key`.

        Serves the snapshot a client gets the moment it subscribes. Without it
        a newly-connected device waits out a poll cycle and then receives only
        the aircraft that happened to *move* — the delta filter suppresses the
        rest until the 60s heartbeat gets to them, so a second device on the
        same map takes up to two minutes to populate.

        A filtered Scan, not a GSI, and deliberately so. DynamoDB bills
        FilterExpression after the read, so this costs a full table read — but
        the table is small (~4k items, ~2.7MB) and subscribes are rare. A GSI
        on region_cell would instead add 1 WRU to *every* position write, and
        writes here run ~25M/month: roughly $31/month per polled cell to serve
        a few hundred snapshots. The scan is about three orders of magnitude
        cheaper. Revisit past ~50k items or if subscribes become continuous.

        Args:
            cell_key: Region cell to snapshot.
            limit: Stop after this many aircraft. A guard, not a page size —
                one cell holds a few hundred, and an unbounded read on a table
                that has grown unexpectedly is how a cheap call becomes costly.

        Returns:
            States with a usable position, newest first is not guaranteed.
        """
        states: list[AircraftState] = []
        kwargs: dict[str, Any] = {
            "FilterExpression": Key("region_cell").eq(cell_key),
        }
        while len(states) < limit:
            response = self._table.scan(**kwargs)
            for item in response.get("Items", []):
                payload = item.get("state")
                if not isinstance(payload, dict):
                    continue
                states.append(
                    AircraftState(
                        **{k: _from_dynamo(v) for k, v in payload.items() if k in _STATE_FIELDS}
                    )
                )
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                break
            kwargs["ExclusiveStartKey"] = start_key
        return states[:limit]

    def put_position(self, state: AircraftState) -> None:
        """Store `state` under its icao24, replacing any prior. Last write wins."""
        item = {
            "icao24": state.icao24,
            "state": {k: _to_dynamo(v) for k, v in asdict(state).items() if v is not None},
            "expires_at": int(self._clock()) + self._ttl_s,
        }
        cell = self._region_cell(state)
        if cell is not None:
            item["region_cell"] = cell

        self._table.put_item(Item=item)


class DynamoConnectionStore:
    """Subscriber registry, satisfying `ConnectionStore`."""

    def __init__(
        self,
        table_name: str = CONNECTIONS_TABLE,
        dynamodb=None,
        ttl_s: int = CONNECTION_TTL_S,
        clock=time.time,
    ) -> None:
        self._table = (dynamodb or boto3.resource("dynamodb")).Table(table_name)
        self._ttl_s = ttl_s
        self._clock = clock

    def list_connections_by_cell(self, cell_key: str) -> Iterable[Connection]:
        """Connections subscribed to `cell_key`; empty iterable, never None.

        Queries the GSI rather than scanning — this runs once per cell per
        poll, so a scan here would make cost scale with total subscribers
        rather than with the ones actually watching this patch of sky.
        """
        connections: list[Connection] = []
        kwargs: dict[str, Any] = {
            "IndexName": REGION_CELL_INDEX,
            "KeyConditionExpression": Key("region_cell").eq(cell_key),
        }
        while True:
            response = self._table.query(**kwargs)
            for item in response.get("Items", []):
                connections.append(
                    Connection(
                        connection_id=item["connection_id"],
                        cell_key=item["region_cell"],
                        connected_at=float(item.get("connected_at", 0)),
                    )
                )
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                return connections
            kwargs["ExclusiveStartKey"] = start_key

    def list_active_cells(self) -> Iterable[str]:
        """Distinct cell keys with at least one subscriber.

        ponytail: a projected scan of the GSI, deduped here. DynamoDB has no
        DISTINCT, and the alternative is an aggregate item updated on every
        connect/disconnect — more moving parts and a write amplifier, to save a
        scan over a table whose size is "currently connected clients". Revisit
        if that number ever reaches thousands.
        """
        cells: set[str] = set()
        kwargs: dict[str, Any] = {
            "IndexName": REGION_CELL_INDEX,
            "ProjectionExpression": "region_cell",
        }
        while True:
            response = self._table.scan(**kwargs)
            for item in response.get("Items", []):
                cells.add(item["region_cell"])
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                return cells
            kwargs["ExclusiveStartKey"] = start_key

    def list_cells_for_connection(self, connection_id: str) -> list[str]:
        """Every cell one connection is subscribed to.

        Beyond the ConnectionStore protocol, which is cell-oriented — but the
        two events that end a subscription (a $disconnect, a 410 from
        postToConnection) both name a connection and no cell. Without this,
        reaping means a table scan or leaving the other cells behind.

        A query on the table's own partition key, so it costs one read
        regardless of how many cells the client covers.
        """
        cells: list[str] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("connection_id").eq(connection_id),
            "ProjectionExpression": "region_cell",
        }
        while True:
            response = self._table.query(**kwargs)
            cells.extend(item["region_cell"] for item in response.get("Items", []))
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                return cells
            kwargs["ExclusiveStartKey"] = start_key

    def delete_all_for_connection(self, connection_id: str) -> int:
        """Remove every subscription held by one connection.

        Returns:
            How many rows were deleted.
        """
        cells = self.list_cells_for_connection(connection_id)
        for cell in cells:
            self.delete_connection(connection_id, cell)
        return len(cells)

    def put_connection(self, connection: Connection) -> None:
        """Register a subscription, replacing any with the same id and cell."""
        self._table.put_item(
            Item={
                "connection_id": connection.connection_id,
                "region_cell": connection.cell_key,
                "connected_at": _to_dynamo(connection.connected_at),
                "expires_at": int(self._clock()) + self._ttl_s,
            }
        )

    def delete_connection(self, connection_id: str, cell_key: str) -> None:
        """Remove a subscription. Idempotent.

        DynamoDB's delete_item is already a no-op on a missing key, which is
        exactly the contract: disconnects arrive late, duplicated, and for rows
        the TTL already reaped.
        """
        self._table.delete_item(
            Key={"connection_id": connection_id, "region_cell": cell_key}
        )

    def count_connections(self, cell_key: Optional[str] = None) -> int:
        """Count subscriptions in `cell_key`, or all cells when None.

        Select=COUNT so DynamoDB counts server-side and returns no items.
        Still paginated: Count is per page, not a grand total, and reading only
        the first page would silently under-report past 1MB of index.
        """
        total = 0
        kwargs: dict[str, Any] = {"IndexName": REGION_CELL_INDEX, "Select": "COUNT"}
        if cell_key is not None:
            kwargs["KeyConditionExpression"] = Key("region_cell").eq(cell_key)

        while True:
            response = (
                self._table.query(**kwargs) if cell_key is not None
                else self._table.scan(**kwargs)
            )
            total += response.get("Count", 0)
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                return total
            kwargs["ExclusiveStartKey"] = start_key

    def clear(self) -> None:
        """Drop every subscription. Used at startup.

        Sockets do not survive a restart, so rows from a previous run are dead
        and would keep the poller fetching cells nobody is watching.
        """
        deleted = 0
        with self._table.batch_writer() as batch:
            kwargs: dict[str, Any] = {"ProjectionExpression": "connection_id, region_cell"}
            while True:
                response = self._table.scan(**kwargs)
                for item in response.get("Items", []):
                    batch.delete_item(
                        Key={
                            "connection_id": item["connection_id"],
                            "region_cell": item["region_cell"],
                        }
                    )
                    deleted += 1
                start_key = response.get("LastEvaluatedKey")
                if not start_key:
                    break
                kwargs["ExclusiveStartKey"] = start_key
        logger.info("cleared %d stale connection rows", deleted)


def table_exists(table_name: str, dynamodb=None) -> bool:
    """True when the table is present and ACTIVE.

    Called at startup so a missing table fails with a sentence naming it,
    rather than a ResourceNotFoundException on the first poll.
    """
    try:
        table = (dynamodb or boto3.resource("dynamodb")).Table(table_name)
        return table.table_status == "ACTIVE"
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            return False
        raise
