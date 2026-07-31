"""Persistence contracts. No implementations, no vendor SDKs.

Protocols rather than ABCs so an adapter — or a dict-backed test fake —
satisfies the contract by shape, without importing this module. Keeps the
dependency arrow pointing inward.
"""

from __future__ import annotations

from typing import Iterable, Optional, Protocol, runtime_checkable

from .models import AircraftState, Connection


@runtime_checkable
class PositionStore(Protocol):
    """Last-known state vector per aircraft. Effectively a keyed cache."""

    def get_position(self, icao24: str) -> Optional[AircraftState]:
        """Last stored state, or None on miss.

        A miss (never seen, or expired) returns None rather than raising —
        callers treat it as a first sighting.
        """
        ...

    def put_position(self, state: AircraftState) -> None:
        """Store `state` under its icao24, replacing any prior. Last write wins."""
        ...


@runtime_checkable
class ConnectionStore(Protocol):
    """Subscriber registry, indexed by region cell.

    list_connections_by_cell runs on every poll, so implementations should make
    it a lookup, not a scan.
    """

    def list_connections_by_cell(self, cell_key: str) -> Iterable[Connection]:
        """Connections subscribed to `cell_key`; empty iterable, never None."""
        ...

    def put_connection(self, connection: Connection) -> None:
        """Register a subscription, replacing any with the same id and cell."""
        ...

    def delete_connection(self, connection_id: str, cell_key: str) -> None:
        """Remove a subscription.

        Must be idempotent: disconnect events arrive late, duplicated, or for
        already-reaped connections.
        """
        ...

    def count_connections(self, cell_key: Optional[str] = None) -> int:
        """Count subscriptions in `cell_key`, or all cells when None.

        Used to skip polling unwatched cells. Advisory — may be stale under
        concurrent connects.
        """
        ...
