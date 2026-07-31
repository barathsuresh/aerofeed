"""Tests for the local backend: SQLite stores, stream and processor.

No network and no real OpenSky calls — the poller's upstream is stubbed.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from core.models import AircraftState, Connection
from core.storage_interface import ConnectionStore, PositionStore
from local.local_stream import LocalStream, StreamRecord
from local.poller import Poller
from local.processor import Processor
from local.sqlite_store import SqliteConnectionStore, SqlitePositionStore, connect

PLANE = AircraftState(
    icao24="a1b2c3",
    callsign="DLH441",
    latitude=40.7,
    longitude=-74.0,
    baro_altitude=10000.0,
    on_ground=False,
)


@pytest.fixture
def db():
    connection = connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture
def positions(db):
    return SqlitePositionStore(db)


@pytest.fixture
def connections(db):
    return SqliteConnectionStore(db)


# --- stores ------------------------------------------------------------------


def test_stores_satisfy_the_phase_one_protocols(positions, connections):
    # The whole point of the SQLite backend: same contract, different engine.
    assert isinstance(positions, PositionStore)
    assert isinstance(connections, ConnectionStore)


def test_position_round_trips_including_nulls(positions):
    sparse = AircraftState(icao24="ffffff")  # every optional field None
    positions.put_position(PLANE)
    positions.put_position(sparse)

    assert positions.get_position("a1b2c3") == PLANE
    assert positions.get_position("ffffff") == sparse


def test_missing_position_returns_none_not_an_error(positions):
    assert positions.get_position("nosuch") is None


def test_put_position_overwrites(positions):
    positions.put_position(PLANE)
    moved = replace(PLANE, latitude=41.0)
    positions.put_position(moved)

    assert positions.get_position("a1b2c3").latitude == 41.0


def test_connection_lifecycle(connections):
    connections.put_connection(Connection("c1", "40_-75", 1.0))
    connections.put_connection(Connection("c2", "40_-75", 2.0))
    connections.put_connection(Connection("c3", "50_0", 3.0))

    assert connections.count_connections() == 3
    assert connections.count_connections("40_-75") == 2
    assert sorted(connections.list_active_cells()) == ["40_-75", "50_0"]
    assert {c.connection_id for c in connections.list_connections_by_cell("40_-75")} == {"c1", "c2"}

    connections.delete_connection("c1", "40_-75")
    assert connections.count_connections("40_-75") == 1

    # Idempotent: late or duplicated disconnects are normal, not errors.
    connections.delete_connection("c1", "40_-75")
    assert connections.count_connections("40_-75") == 1


def test_same_client_can_subscribe_to_two_cells(connections):
    connections.put_connection(Connection("c1", "40_-75", 1.0))
    connections.put_connection(Connection("c1", "50_0", 1.0))
    assert connections.count_connections() == 2


def test_empty_cell_lists_nothing(connections):
    assert list(connections.list_connections_by_cell("0_0")) == []
    assert list(connections.list_active_cells()) == []


def test_clear_drops_stale_rows_from_a_previous_run(connections):
    connections.put_connection(Connection("c1", "40_-75", 1.0))
    connections.clear()
    assert connections.count_connections() == 0


# --- processor ---------------------------------------------------------------


class SpyBroadcast:
    """Records what the processor decided to push."""

    def __init__(self):
        self.records: list[StreamRecord] = []

    async def __call__(self, record: StreamRecord) -> int:
        self.records.append(record)
        return 1


async def test_first_sighting_is_stored_and_broadcast(positions):
    spy = SpyBroadcast()
    processor = Processor(positions, spy)

    assert await processor.process(StreamRecord(PLANE, "40_-75")) is True
    assert len(spy.records) == 1
    assert positions.get_position("a1b2c3") == PLANE


async def test_unchanged_state_is_neither_stored_nor_broadcast(positions):
    spy = SpyBroadcast()
    processor = Processor(positions, spy)

    await processor.process(StreamRecord(PLANE, "40_-75"))
    jitter = replace(PLANE, latitude=PLANE.latitude + 0.0001)
    assert await processor.process(StreamRecord(jitter, "40_-75")) is False

    assert len(spy.records) == 1  # still just the first sighting
    assert positions.get_position("a1b2c3").latitude == PLANE.latitude


class FakeClock:
    """Manually advanced monotonic clock, so heartbeat tests never sleep."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def test_stationary_aircraft_is_resent_by_heartbeat(positions):
    """A parked aircraft must not silently age out of a client's map."""
    clock = FakeClock()
    spy = SpyBroadcast()
    processor = Processor(positions, spy, heartbeat_s=60.0, clock=clock)
    parked = replace(PLANE, on_ground=True, velocity=0.0)

    assert await processor.process(StreamRecord(parked, "40_-75")) is True  # first sighting

    # Polls at 15s intervals: unchanged, and not yet due.
    for _ in range(3):
        clock.advance(15.0)
        assert await processor.process(StreamRecord(parked, "40_-75")) is False
    assert len(spy.records) == 1

    # 60s since the last send — resent even though nothing changed.
    clock.advance(15.0)
    assert await processor.process(StreamRecord(parked, "40_-75")) is True
    assert len(spy.records) == 2


async def test_heartbeat_does_not_move_the_delta_baseline(positions):
    """Sub-threshold drift must still accumulate across heartbeats.

    If a heartbeat overwrote the stored position, each one would reset the
    reference point and an aircraft creeping just under the threshold would
    never be reported as moving.
    """
    clock = FakeClock()
    spy = SpyBroadcast()
    processor = Processor(positions, spy, heartbeat_s=60.0, clock=clock)

    await processor.process(StreamRecord(PLANE, "40_-75"))
    baseline = positions.get_position("a1b2c3").latitude

    # Creep 0.002 deg per 15s poll — slow enough that the 60s heartbeat fires
    # before the 0.01 threshold is ever crossed. That ordering is the whole
    # point: it is the only way to observe whether a heartbeat writes.
    creeping = PLANE
    for _ in range(4):
        clock.advance(15.0)
        creeping = replace(creeping, latitude=creeping.latitude + 0.002)
        await processor.process(StreamRecord(creeping, "40_-75"))

    # t+60s: the heartbeat has fired (2 sends), but drift is still only 0.008.
    assert len(spy.records) == 2
    assert positions.get_position("a1b2c3").latitude == pytest.approx(baseline)

    # Keep creeping until the accumulated drift trips the 0.01 threshold.
    for _ in range(2):
        clock.advance(15.0)
        creeping = replace(creeping, latitude=creeping.latitude + 0.002)
        await processor.process(StreamRecord(creeping, "40_-75"))

    # Tripped ~0.010 past the ORIGINAL baseline. That is the discriminating
    # assertion: had the heartbeat written its 0.008 position, movement would
    # be measured from there and would not trip until ~0.018 past baseline.
    assert positions.get_position("a1b2c3").latitude == pytest.approx(
        baseline + 0.010, abs=1e-6
    )


async def test_real_movement_is_broadcast(positions):
    spy = SpyBroadcast()
    processor = Processor(positions, spy)

    await processor.process(StreamRecord(PLANE, "40_-75"))
    moved = replace(PLANE, latitude=PLANE.latitude + 0.5)
    assert await processor.process(StreamRecord(moved, "40_-75")) is True

    assert len(spy.records) == 2
    assert positions.get_position("a1b2c3").latitude == moved.latitude


# --- stream ------------------------------------------------------------------


async def test_stream_delivers_published_records():
    stream = LocalStream()
    seen = []

    consumer = asyncio.create_task(stream.consume(lambda r: _collect(seen, r)))
    await stream.publish(StreamRecord(PLANE, "40_-75"))
    await asyncio.sleep(0)  # let the consumer run
    consumer.cancel()

    assert [r.state.icao24 for r in seen] == ["a1b2c3"]


async def test_consumer_survives_a_failing_processor():
    """One bad record must not stall every subscriber."""
    stream = LocalStream()
    seen = []

    async def flaky(record):
        if record.state.icao24 == "bad":
            raise ValueError("boom")
        seen.append(record)

    consumer = asyncio.create_task(stream.consume(flaky))
    await stream.publish(StreamRecord(replace(PLANE, icao24="bad"), "40_-75"))
    await stream.publish(StreamRecord(PLANE, "40_-75"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    consumer.cancel()

    assert [r.state.icao24 for r in seen] == ["a1b2c3"]


async def _collect(sink, record):
    sink.append(record)


# --- poller ------------------------------------------------------------------


class StubClient:
    """Stands in for OpenSkyClient; records the boxes it was asked for."""

    def __init__(self, states):
        self._states = states
        self.calls = []

    def get_states(self, lamin, lomin, lamax, lomax):
        self.calls.append((lamin, lomin, lamax, lomax))
        return self._states


async def test_poller_polls_only_cells_with_subscribers(connections):
    stream = LocalStream()
    client = StubClient([PLANE])
    poller = Poller(client, connections, stream)

    # Nobody connected: zero upstream calls. This is the whole cost argument.
    assert await poller.poll_once() == 0
    assert client.calls == []

    connections.put_connection(Connection("c1", "40_-75", 1.0))
    assert await poller.poll_once() == 1
    assert client.calls == [(40.0, -75.0, 45.0, -70.0)]


async def test_poller_calls_once_per_cell_not_per_client(connections):
    stream = LocalStream()
    client = StubClient([PLANE])
    poller = Poller(client, connections, stream)

    for i in range(5):
        connections.put_connection(Connection(f"c{i}", "40_-75", 1.0))

    await poller.poll_once()
    assert len(client.calls) == 1


async def test_poller_tags_records_with_their_cell(connections):
    stream = LocalStream()
    poller = Poller(StubClient([PLANE]), connections, stream)
    connections.put_connection(Connection("c1", "40_-75", 1.0))

    await poller.poll_once()
    seen = []
    consumer = asyncio.create_task(stream.consume(lambda r: _collect(seen, r)))
    await asyncio.sleep(0)
    consumer.cancel()

    assert seen[0].region_cell == "40_-75"
