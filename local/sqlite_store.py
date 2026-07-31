"""SQLite implementations of the phase-1 storage protocols.

Same contract as the eventual DynamoDB adapters, different backend. The schema
is shaped like the DynamoDB tables it stands in for — `connections` is keyed
(cell_key, connection_id) so the hot query is an index hit rather than a scan,
exactly as a partition-key lookup would be — so swapping backends later is a
new file, not a redesign.

ponytail: sqlite3 is blocking and these calls run on the event loop. At local
volumes (one poll every 15s, a handful of clients) that is sub-millisecond and
a thread pool would be pure ceremony. Move to aiosqlite, or run the store in a
worker thread, if the loop ever stalls.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, fields
from pathlib import Path
from typing import Iterable, Optional

from core.models import AircraftState, Connection

# Field names accepted by the AircraftState constructor. Rows written by an
# older build can carry keys that no longer exist; filtering on read means a
# stale local.db degrades instead of crashing on startup.
_STATE_FIELDS = {f.name for f in fields(AircraftState)}

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    icao24 TEXT PRIMARY KEY,
    state  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connections (
    cell_key      TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    connected_at  REAL NOT NULL,
    PRIMARY KEY (cell_key, connection_id)
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    """Open the local database and ensure the schema exists.

    Args:
        path: Database file, or ":memory:" for tests.

    Returns:
        An autocommit connection shared by both stores.
    """
    # isolation_level=None -> autocommit. Every write here is a single
    # statement, so explicit transactions would only add a way to forget one.
    connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


class SqlitePositionStore:
    """Last-known state vector per aircraft, satisfying `PositionStore`."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    def get_position(self, icao24: str) -> Optional[AircraftState]:
        """Last stored state, or None on miss."""
        row = self._db.execute(
            "SELECT state FROM positions WHERE icao24 = ?", (icao24,)
        ).fetchone()
        if row is None:
            return None

        payload = json.loads(row["state"])
        return AircraftState(**{k: v for k, v in payload.items() if k in _STATE_FIELDS})

    def put_position(self, state: AircraftState) -> None:
        """Store `state` under its icao24, replacing any prior."""
        self._db.execute(
            "INSERT INTO positions (icao24, state) VALUES (?, ?) "
            "ON CONFLICT(icao24) DO UPDATE SET state = excluded.state",
            (state.icao24, json.dumps(asdict(state))),
        )


class SqliteConnectionStore:
    """Subscriber registry, satisfying `ConnectionStore`.

    Holds subscription *metadata* only. Live socket objects are not persistable
    and stay in `local_ws_server`'s in-process registry.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    def list_connections_by_cell(self, cell_key: str) -> Iterable[Connection]:
        """Connections subscribed to `cell_key`; empty list when none."""
        rows = self._db.execute(
            "SELECT connection_id, cell_key, connected_at FROM connections "
            "WHERE cell_key = ?",
            (cell_key,),
        ).fetchall()
        return [Connection(**dict(row)) for row in rows]

    def list_active_cells(self) -> Iterable[str]:
        """Distinct cell keys with at least one subscriber."""
        rows = self._db.execute("SELECT DISTINCT cell_key FROM connections").fetchall()
        return [row["cell_key"] for row in rows]

    def put_connection(self, connection: Connection) -> None:
        """Register a subscription, replacing any with the same id and cell."""
        self._db.execute(
            "INSERT INTO connections (cell_key, connection_id, connected_at) "
            "VALUES (?, ?, ?) ON CONFLICT(cell_key, connection_id) "
            "DO UPDATE SET connected_at = excluded.connected_at",
            (connection.cell_key, connection.connection_id, connection.connected_at),
        )

    def delete_connection(self, connection_id: str, cell_key: str) -> None:
        """Remove a subscription. Idempotent — deleting nothing is not an error."""
        self._db.execute(
            "DELETE FROM connections WHERE cell_key = ? AND connection_id = ?",
            (cell_key, connection_id),
        )

    def count_connections(self, cell_key: Optional[str] = None) -> int:
        """Count subscriptions in `cell_key`, or all cells when None."""
        if cell_key is None:
            row = self._db.execute("SELECT COUNT(*) AS n FROM connections").fetchone()
        else:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM connections WHERE cell_key = ?", (cell_key,)
            ).fetchone()
        return int(row["n"])

    def clear(self) -> None:
        """Drop every subscription.

        Called at startup: sockets do not survive a restart, so any rows left
        by a previous run are dead and would keep the poller alive with no
        listeners.
        """
        self._db.execute("DELETE FROM connections")
