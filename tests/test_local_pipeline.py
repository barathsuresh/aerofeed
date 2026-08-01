"""Tests for the local backend: SQLite stores, stream and processor.

No network and no real upstream calls — the poller's client is stubbed.
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import replace

import pytest

from core.airplanes_live_client import MAX_RADIUS_NM
from core.geo import DEFAULT_MAX_CELLS, snap_to_grid
from core.models import AircraftState, Connection
from core.storage_interface import ConnectionStore, PositionStore
from local.local_stream import LocalStream, StreamRecord
from local.local_scheduler import LocalScheduler
from local.local_ws_server import LocalWebSocketServer
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


def test_positions_can_be_listed_by_region_cell(positions):
    """Local snapshots use the same cell attribute DynamoDB stores."""
    london = replace(PLANE, icao24="london", latitude=51.5, longitude=-0.1)
    unmapped = AircraftState(icao24="nopos")
    positions.put_position(PLANE)
    positions.put_position(london)
    positions.put_position(unmapped)

    assert [state.icao24 for state in positions.list_positions_in_cell("40_-75")] == ["a1b2c3"]
    assert [state.icao24 for state in positions.list_positions_in_cell("50_-5")] == ["london"]


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
    """Stands in for AirplanesLiveClient; records the circles it was asked for."""

    def __init__(self, states):
        self._states = states
        self.calls = []

    def get_states(self, lat, lon, radius_nm):
        self.calls.append((lat, lon, radius_nm))
        return self._states


def _poller(client, connections, stream):
    """Poller with rate-limit pacing disabled — tests must not sleep 1.1s.

    Pacing is asserted directly in test_poller_paces_calls_for_the_rate_limit.
    """
    return Poller(client, connections, stream, inter_call_delay_s=0)


async def test_poller_polls_only_cells_with_subscribers(connections):
    stream = LocalStream()
    client = StubClient([PLANE])
    poller = _poller(client, connections, stream)

    # Nobody connected: zero upstream calls. This is the whole cost argument.
    assert await poller.poll_once() == 0
    assert client.calls == []

    connections.put_connection(Connection("c1", "40_-75", 1.0))
    assert await poller.poll_once() == 1

    # Cell 40..45 N, -75..-70 E -> centre of the box, radius covering its
    # corners: hypot(2.5 * 60, 2.5 * 60 * cos(40 deg)) = hypot(150, 114.9).
    (lat, lon, radius_nm), = client.calls
    assert (lat, lon) == (42.5, -72.5)
    assert radius_nm == pytest.approx(188.95, abs=0.05)


async def test_poller_circle_covers_the_whole_cell(connections):
    """Under-covering leaves a corner of a client's map permanently empty."""
    stream = LocalStream()
    client = StubClient([])
    connections.put_connection(Connection("c1", "40_-75", 1.0))

    await _poller(client, connections, stream).poll_once()
    lat, lon, radius_nm = client.calls[0]

    # Every corner of the cell must fall inside the requested circle.
    for corner_lat, corner_lon in ((40.0, -75.0), (40.0, -70.0), (45.0, -75.0), (45.0, -70.0)):
        north_nm = (corner_lat - lat) * 60.0
        # Longitude degrees are widest at the corner nearest the equator.
        east_nm = (corner_lon - lon) * 60.0 * math.cos(math.radians(min(abs(corner_lat), abs(lat))))
        assert math.hypot(north_nm, east_nm) <= radius_nm + 1e-9


async def test_poller_never_exceeds_the_providers_radius_limit(connections):
    """A too-large radius is a 400, i.e. a silently lost poll."""
    stream = LocalStream()
    client = StubClient([])
    poller = Poller(client, connections, stream, grid_size_degrees=5, inter_call_delay_s=0)
    connections.put_connection(Connection("c1", "40_-75", 1.0))

    await poller.poll_once()

    assert client.calls[0][2] <= MAX_RADIUS_NM


async def test_poller_calls_once_per_cell_not_per_client(connections):
    stream = LocalStream()
    client = StubClient([PLANE])
    poller = _poller(client, connections, stream)

    for i in range(5):
        connections.put_connection(Connection(f"c{i}", "40_-75", 1.0))

    await poller.poll_once()
    assert len(client.calls) == 1


async def test_poller_paces_calls_for_the_rate_limit(connections, monkeypatch):
    """1 req/s is the provider's hard limit; the caller owns the pacing.

    Sleeps are recorded rather than performed — the assertion is about how many
    gaps there are and how long, not about wall clock.
    """
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    stream = LocalStream()
    client = StubClient([])
    poller = Poller(client, connections, stream)  # real 1.1s delay

    for key, (lat, lon) in {"40_-75": (42, -72), "50_0": (52, 2), "0_0": (2, 2)}.items():
        connections.put_connection(Connection(f"c-{key}", key, 1.0))

    await poller.poll_once()

    # Three cells, two gaps: between calls, never before the first or after the
    # last. Paying a delay for a lone cell would halve a single-client's cadence.
    assert len(client.calls) == 3
    assert slept == [1.1, 1.1]


async def test_single_cell_poll_does_not_sleep(connections, monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    stream = LocalStream()
    connections.put_connection(Connection("c1", "40_-75", 1.0))

    await Poller(StubClient([]), connections, LocalStream()).poll_once()

    assert slept == []


async def test_poller_tags_records_with_their_cell(connections):
    stream = LocalStream()
    poller = _poller(StubClient([PLANE]), connections, stream)
    connections.put_connection(Connection("c1", "40_-75", 1.0))

    await poller.poll_once()
    seen = []
    consumer = asyncio.create_task(stream.consume(lambda r: _collect(seen, r)))
    await asyncio.sleep(0)
    consumer.cancel()

    assert seen[0].region_cell == "40_-75"


# --- resubscription (panning the map) ----------------------------------------


class FakeSocket:
    """Minimal ServerConnection stand-in: records what was sent to the client."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, message):
        self.sent.append(json.loads(message))


def _server(connections):
    return LocalWebSocketServer(connections, default_lat=40.7, default_lon=-74.0)


async def _subscribe(server, socket, connections, cells, lat, lon):
    """Drive one point-form subscribe message through the handler."""
    return await server._handle_message(
        socket, "c1", cells, json.dumps({"type": "subscribe", "lat": lat, "lon": lon})
    )


async def _subscribe_bounds(server, socket, cells, bounds):
    """Drive one viewport-form subscribe message through the handler."""
    return await server._handle_message(
        socket, "c1", cells, json.dumps({"type": "subscribe", "bounds": list(bounds)})
    )


async def test_panning_into_a_new_cell_moves_the_subscription(connections):
    """The reported bug: without this the poller is only ever asked about the
    cell chosen at connect time, so a panned map shows empty sky."""
    server = _server(connections)
    socket = FakeSocket()
    cell = snap_to_grid(40.7, -74.0)
    cells = {cell.key}
    connections.put_connection(Connection("c1", cell.key, 1.0))

    moved = await _subscribe(server, socket, connections, cells, 51.5, -0.1)

    assert moved == {"50_-5"}
    assert sorted(connections.list_active_cells()) == ["50_-5"]
    assert socket.sent[-1]["type"] == "subscribed"
    assert socket.sent[-1]["region_cell"] == "50_-5"


async def test_local_subscribe_sends_snapshot_for_new_cells(connections, positions):
    """Local and Lambda should both populate a newly-covered cell immediately."""
    london = replace(PLANE, latitude=51.5, longitude=-0.1)
    positions.put_position(london)
    server = LocalWebSocketServer(
        connections, 40.7, -74.0, position_store=positions
    )
    socket = FakeSocket()
    cell = snap_to_grid(40.7, -74.0)
    cells = {cell.key}
    connections.put_connection(Connection("c1", cell.key, 1.0))

    await _subscribe(server, socket, connections, cells, 51.5, -0.1)

    aircraft = [frame for frame in socket.sent if frame["type"] == "aircraft"]
    assert len(aircraft) == 1
    assert aircraft[0]["region_cell"] == "50_-5"
    assert aircraft[0]["snapshot"] is True


async def test_the_old_cell_stops_being_polled(connections):
    """Otherwise every pan permanently adds an upstream request per cycle."""
    server = _server(connections)
    cell = snap_to_grid(40.7, -74.0)
    cells = {cell.key}
    connections.put_connection(Connection("c1", cell.key, 1.0))

    await _subscribe(server, FakeSocket(), connections, cells, 51.5, -0.1)

    assert cell.key not in connections.list_active_cells()
    assert connections.count_connections() == 1  # moved, not duplicated


async def test_panning_within_the_same_cell_is_a_no_op(connections):
    """The client re-sends on every move because it cannot know where the
    boundaries are; the server is what makes that cheap."""
    server = _server(connections)
    socket = FakeSocket()
    cell = snap_to_grid(40.7, -74.0)
    cells = {cell.key}
    connections.put_connection(Connection("c1", cell.key, 1.0))

    same = await _subscribe(server, socket, connections, cells, 42.0, -72.0)

    assert same == cells
    assert socket.sent == []  # no needless round trip
    assert connections.count_connections() == 1


@pytest.mark.parametrize(
    "raw",
    [
        "not json", "[]", "null", '{"type":"nonsense"}', '{"type":"subscribe"}',
        '{"type":"subscribe","lat":"x","lon":0}', '{"type":"subscribe","lat":999,"lon":0}',
        '{"type":"subscribe","lat":null,"lon":null}',
    ],
)
async def test_a_bad_message_leaves_the_subscription_untouched(connections, raw):
    """A client sending nonsense keeps the aircraft it already has."""
    server = _server(connections)
    socket = FakeSocket()
    cell = snap_to_grid(40.7, -74.0)
    cells = {cell.key}
    connections.put_connection(Connection("c1", cell.key, 1.0))

    unchanged = await server._handle_message(socket, "c1", cells, raw)

    assert unchanged == cells
    assert list(connections.list_active_cells()) == [cell.key]
    assert socket.sent == []


async def test_poller_follows_the_client_to_the_new_cell(connections):
    """End to end: resubscribing is what makes the new sky get polled at all."""
    stream = LocalStream()
    client = StubClient([PLANE])
    poller = _poller(client, connections, stream)
    server = _server(connections)
    cell = snap_to_grid(40.7, -74.0)
    cells = {cell.key}
    connections.put_connection(Connection("c1", cell.key, 1.0))

    await poller.poll_once()
    first = client.calls[0]

    await _subscribe(server, FakeSocket(), connections, cells, 51.5, -0.1)
    await poller.poll_once()

    assert len(client.calls) == 2
    assert client.calls[1] != first
    assert client.calls[1][:2] == (52.5, -2.5)  # centre of cell 50_-5


async def test_a_wide_viewport_subscribes_to_every_cell_it_spans(connections):
    """The zoomed-out complaint: a viewport spanning four cells used to get
    one cell's aircraft, so most of the map sat empty."""
    server = _server(connections)
    socket = FakeSocket()
    start = snap_to_grid(40.7, -74.0)
    cells = {start.key}
    connections.put_connection(Connection("c1", start.key, 1.0))

    updated = await _subscribe_bounds(server, socket, cells, (38.0, -76.0, 47.0, -68.0))

    assert len(updated) == DEFAULT_MAX_CELLS
    assert sorted(connections.list_active_cells()) == sorted(updated)
    assert len(socket.sent[-1]["cells"]) == DEFAULT_MAX_CELLS


async def test_the_poller_fetches_every_subscribed_cell(connections):
    """Coverage only counts if the cells are actually polled."""
    stream = LocalStream()
    client = StubClient([PLANE])
    server = _server(connections)
    start = snap_to_grid(40.7, -74.0)
    connections.put_connection(Connection("c1", start.key, 1.0))

    updated = await _subscribe_bounds(server, FakeSocket(), {start.key}, (38.0, -76.0, 47.0, -68.0))
    await _poller(client, connections, stream).poll_once()

    assert len(client.calls) == len(updated) == DEFAULT_MAX_CELLS


async def test_the_client_is_told_when_coverage_is_capped(connections):
    """Otherwise a capped viewport is indistinguishable from a dead feed."""
    server = LocalWebSocketServer(connections, 40.7, -74.0, max_cells=4)
    socket = FakeSocket()
    start = snap_to_grid(40.7, -74.0)
    connections.put_connection(Connection("c1", start.key, 1.0))

    await _subscribe_bounds(server, socket, {start.key}, (-60.0, -170.0, 60.0, 170.0))

    assert socket.sent[-1]["truncated"] is True
    assert socket.sent[-1]["max_cells"] == 4
    assert len(socket.sent[-1]["cells"]) == 4


async def test_a_narrow_viewport_is_not_reported_as_capped(connections):
    server = LocalWebSocketServer(connections, 40.7, -74.0, max_cells=DEFAULT_MAX_CELLS)
    socket = FakeSocket()
    # Start elsewhere, so moving here is a real change and does send a reply.
    start = snap_to_grid(0.0, 0.0)
    connections.put_connection(Connection("c1", start.key, 1.0))

    updated = await _subscribe_bounds(server, socket, {start.key}, (41.0, -74.0, 42.0, -73.0))

    assert updated == {"40_-75"}
    assert socket.sent[-1]["truncated"] is False


async def test_shrinking_the_viewport_releases_the_cells_it_left(connections):
    """Otherwise every zoom-out permanently adds requests to every poll cycle."""
    server = _server(connections)
    start = snap_to_grid(40.7, -74.0)
    connections.put_connection(Connection("c1", start.key, 1.0))

    wide = await _subscribe_bounds(server, FakeSocket(), {start.key}, (38.0, -76.0, 47.0, -68.0))
    assert len(wide) == DEFAULT_MAX_CELLS

    narrow = await _subscribe_bounds(server, FakeSocket(), wide, (41.0, -74.0, 42.0, -73.0))

    assert narrow == {"40_-75"}
    assert sorted(connections.list_active_cells()) == ["40_-75"]
    assert connections.count_connections() == 1


async def test_disconnect_releases_every_cell(connections):
    """A multi-cell client that vanishes must not leave the poller working."""
    server = _server(connections)
    start = snap_to_grid(40.7, -74.0)
    connections.put_connection(Connection("c1", start.key, 1.0))
    cells = await _subscribe_bounds(server, FakeSocket(), {start.key}, (38.0, -76.0, 47.0, -68.0))

    for key in cells:
        connections.delete_connection("c1", key)

    assert list(connections.list_active_cells()) == []


async def test_subscribing_wakes_the_scheduler(connections):
    """Without the wake, sky the user just panned to stays blank for a full
    interval, which reads as broken rather than as pending."""
    woken = []
    server = LocalWebSocketServer(connections, 40.7, -74.0, on_subscribe=lambda: woken.append(1))
    start = snap_to_grid(40.7, -74.0)
    connections.put_connection(Connection("c1", start.key, 1.0))

    await _subscribe_bounds(server, FakeSocket(), {start.key}, (38.0, -76.0, 47.0, -68.0))
    assert len(woken) == 1

    # A no-op resubscribe must not wake it — a drag inside one cell would
    # otherwise poll on every mouse-up.
    await _subscribe_bounds(server, FakeSocket(), {"40_-75"}, (41.0, -74.0, 42.0, -73.0))
    assert len(woken) == 1


async def test_scheduler_polls_early_when_woken(connections):
    """The wake shortens the sleep rather than being merely recorded."""
    connections.put_connection(Connection("c1", "40_-75", 1.0))

    class CountingPoller:
        def __init__(self):
            self.polls = 0

        async def poll_once(self):
            self.polls += 1
            return 0

    poller = CountingPoller()
    scheduler = LocalScheduler(poller, connections, interval_s=3600)  # never on its own
    task = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0)
    assert poller.polls == 1  # the immediate first tick

    scheduler.wake()
    for _ in range(5):
        await asyncio.sleep(0)

    assert poller.polls == 2  # woken, not waited out
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_the_server_exposes_the_methods_run_local_wires(connections):
    """run_local.py calls .serve() and hands .broadcast to the Processor. Neither
    is reachable from the handler tests, so a refactor can delete them silently —
    as one did."""
    server = _server(connections)
    for name in ("handler", "broadcast", "serve", "_handle_message"):
        assert callable(getattr(server, name, None)), f"{name} is missing"


async def test_broadcast_reaches_only_the_matching_cell(connections):
    server = _server(connections)
    sent = []

    class Sock:
        def __init__(self, tag): self.tag = tag
        async def send(self, m): sent.append((self.tag, json.loads(m)["state"]["icao24"]))

    connections.put_connection(Connection("here", "40_-75", 1.0))
    connections.put_connection(Connection("elsewhere", "50_0", 1.0))
    server._sockets["here"] = Sock("here")
    server._sockets["elsewhere"] = Sock("elsewhere")

    delivered = await server.broadcast(StreamRecord(PLANE, "40_-75"))

    assert delivered == 1
    assert sent == [("here", "a1b2c3")]
