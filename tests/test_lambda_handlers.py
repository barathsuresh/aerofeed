"""Handler tests driven by synthetic event dicts. No AWS, no network.

Every handler is called exactly as Lambda would call it — `handler(event,
context)` — with fakes standing in for DynamoDB, Kinesis, EventBridge and the
API Gateway Management API. That is the point of keeping them thin: the event
shapes are pinned here, and the logic underneath is already covered by the
core and pipeline suites.

Clients in lambdas/_common.py are built through lru_cached accessors rather
than at import, so these modules import cleanly with no AWS region or
credentials configured — which is what makes this file runnable in CI. Tests
patch the accessor, not a module-level object.
"""

from __future__ import annotations

import base64
import json
from unittest import mock

import pytest

from aws.kinesis_stream import encode
from core.geo import DEFAULT_MAX_CELLS
from core.models import AircraftState, Connection
from local.local_stream import StreamRecord

PLANE = AircraftState(
    icao24="4caec4", callsign="CGI120F", latitude=51.5, longitude=-0.1,
    baro_altitude=5486.4, on_ground=False, registration="EI-IRF",
    aircraft_type="BE20", position_source="live",
)


class FakeConnectionStore:
    """In-memory ConnectionStore with the two extra methods the handlers use."""

    def __init__(self, rows=()):
        self.rows: set[tuple[str, str]] = set(rows)

    def put_connection(self, connection: Connection) -> None:
        self.rows.add((connection.connection_id, connection.cell_key))

    def delete_connection(self, connection_id: str, cell_key: str) -> None:
        self.rows.discard((connection_id, cell_key))

    def count_connections(self, cell_key=None) -> int:
        return len([r for r in self.rows if cell_key is None or r[1] == cell_key])

    def list_active_cells(self):
        return {cell for _, cell in self.rows}

    def list_connections_by_cell(self, cell_key):
        return [Connection(cid, cell, 1.0) for cid, cell in self.rows if cell == cell_key]

    def list_cells_for_connection(self, connection_id):
        return [cell for cid, cell in self.rows if cid == connection_id]

    def delete_all_for_connection(self, connection_id) -> int:
        cells = self.list_cells_for_connection(connection_id)
        for cell in cells:
            self.delete_connection(connection_id, cell)
        return len(cells)


class FakePositionStore:
    def __init__(self):
        self.items: dict[str, AircraftState] = {}

    def get_position(self, icao24):
        return self.items.get(icao24)

    def put_position(self, state):
        self.items[state.icao24] = state


def client_error(code):
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": code, "Message": code}}, "PostToConnection")


# --- connect ------------------------------------------------------------------


@pytest.fixture
def connect_mod():
    from lambdas import connect_handler
    return connect_handler


def connect_event(connection_id="abc", source_ip="49.207.1.1", params=None):
    return {
        "requestContext": {
            "connectionId": connection_id,
            "identity": {"sourceIp": source_ip},
        },
        "queryStringParameters": params,
    }


def test_connect_registers_the_client_in_its_geoip_cell(connect_mod):
    store = FakeConnectionStore()
    with mock.patch.object(connect_mod, "get_connection_store", lambda: store), \
         mock.patch.object(connect_mod, "geoip", return_value=(13.08, 80.27, "Chennai")), \
         mock.patch.object(connect_mod, "enable_polling") as enable, \
         mock.patch.object(connect_mod, "invoke_initial_poll") as invoke:
        assert connect_mod.handler(connect_event(), None) == {"statusCode": 200}

    assert store.rows == {("abc", "10_80")}
    enable.assert_called_once()  # first connection wakes the poller
    invoke.assert_called_once()  # and does not wait for the next minute tick


def test_connect_prefers_an_explicit_query_override_over_geoip(connect_mod):
    """A client may want to watch sky it is not sitting under."""
    store = FakeConnectionStore()
    with mock.patch.object(connect_mod, "get_connection_store", lambda: store), \
         mock.patch.object(connect_mod, "geoip") as geo, \
         mock.patch.object(connect_mod, "enable_polling"), \
         mock.patch.object(connect_mod, "invoke_initial_poll"):
        connect_mod.handler(connect_event(params={"lat": "51.5", "lon": "-0.1"}), None)

    assert store.rows == {("abc", "50_-5")}
    geo.assert_not_called()  # no external call when we were told where to look


def test_connect_falls_back_to_the_default_when_geoip_cannot_place_the_ip(connect_mod):
    """Private ranges and reserved IPs answer "fail" — normal, not an error."""
    store = FakeConnectionStore()
    with mock.patch.object(connect_mod, "get_connection_store", lambda: store), \
         mock.patch.object(connect_mod, "geoip", return_value=None), \
         mock.patch.object(connect_mod, "enable_polling"), \
         mock.patch.object(connect_mod, "invoke_initial_poll"):
        connect_mod.handler(connect_event(source_ip="127.0.0.1"), None)

    assert store.rows == {("abc", "40_-75")}  # DEFAULT_LAT/LON -> New York


def test_connect_does_not_re_enable_polling_when_others_are_present(connect_mod):
    store = FakeConnectionStore({("existing", "50_-5")})
    with mock.patch.object(connect_mod, "get_connection_store", lambda: store), \
         mock.patch.object(connect_mod, "geoip", return_value=(51.5, -0.1, "London")), \
         mock.patch.object(connect_mod, "enable_polling") as enable, \
         mock.patch.object(connect_mod, "invoke_initial_poll") as invoke:
        connect_mod.handler(connect_event(connection_id="second"), None)

    enable.assert_not_called()
    invoke.assert_not_called()


def test_connect_rejects_an_event_without_a_connection_id(connect_mod):
    assert connect_mod.handler({"requestContext": {}}, None)["statusCode"] == 400


def test_connect_rejects_out_of_range_coordinates(connect_mod):
    """A hand-edited query string must not become an unpollable cell."""
    store = FakeConnectionStore()
    with mock.patch.object(connect_mod, "get_connection_store", lambda: store):
        result = connect_mod.handler(connect_event(params={"lat": "999", "lon": "0"}), None)

    assert result["statusCode"] == 400
    assert store.rows == set()


def test_a_failed_geoip_lookup_never_breaks_the_connection(connect_mod):
    """A third party being down must not stop people connecting."""
    with mock.patch.object(connect_mod.urllib.request, "urlopen", side_effect=OSError("boom")):
        assert connect_mod.geoip("49.207.1.1") is None


def test_geoip_reports_none_for_an_unplaceable_address(connect_mod):
    body = mock.MagicMock()
    body.read.return_value = b'{"status":"fail","message":"private range"}'
    body.__enter__.return_value = body
    with mock.patch.object(connect_mod.urllib.request, "urlopen", return_value=body):
        assert connect_mod.geoip("10.0.0.1") is None


def test_geoip_parses_a_successful_lookup(connect_mod):
    body = mock.MagicMock()
    body.read.return_value = b'{"status":"success","lat":13.08,"lon":80.27,"city":"Chennai"}'
    body.__enter__.return_value = body
    with mock.patch.object(connect_mod.urllib.request, "urlopen", return_value=body):
        assert connect_mod.geoip("49.207.1.1") == (13.08, 80.27, "Chennai")


# --- disconnect ---------------------------------------------------------------


@pytest.fixture
def disconnect_mod():
    from lambdas import disconnect_handler
    return disconnect_handler


def test_disconnect_removes_every_cell_the_client_held(disconnect_mod):
    """A wide-zoom client holds one row per cell and $disconnect names none."""
    store = FakeConnectionStore({("abc", f"40_{lon}") for lon in (-80, -75, -70)})
    store.rows.add(("other", "50_-5"))

    with mock.patch.object(disconnect_mod, "get_connection_store", lambda: store), \
         mock.patch.object(disconnect_mod, "schedule_grace_check") as grace:
        result = disconnect_mod.handler(
            {"requestContext": {"connectionId": "abc"}}, None
        )

    assert result == {"statusCode": 200}
    assert store.rows == {("other", "50_-5")}
    grace.assert_not_called()  # someone is still connected


def test_disconnect_arms_the_grace_check_when_the_last_client_leaves(disconnect_mod):
    store = FakeConnectionStore({("abc", "50_-5")})
    with mock.patch.object(disconnect_mod, "get_connection_store", lambda: store), \
         mock.patch.object(disconnect_mod, "schedule_grace_check") as grace:
        disconnect_mod.handler({"requestContext": {"connectionId": "abc"}}, None)

    grace.assert_called_once()


def test_disconnect_is_idempotent(disconnect_mod):
    """Disconnects arrive late, duplicated, and for rows a 410 already reaped."""
    store = FakeConnectionStore()
    with mock.patch.object(disconnect_mod, "get_connection_store", lambda: store), \
         mock.patch.object(disconnect_mod, "schedule_grace_check"):
        for _ in range(3):
            assert disconnect_mod.handler(
                {"requestContext": {"connectionId": "ghost"}}, None
            )["statusCode"] == 200


def test_grace_schedule_is_one_shot_and_self_deleting(disconnect_mod):
    """Without ActionAfterCompletion=DELETE every disconnect leaves a spent
    schedule behind until the account fills with them."""
    fake = mock.MagicMock()
    with mock.patch.object(disconnect_mod, "get_scheduler", lambda: fake), \
         mock.patch.object(disconnect_mod, "GRACE_TARGET_ARN", "arn:aws:lambda:::fn"), \
         mock.patch.object(disconnect_mod, "GRACE_ROLE_ARN", "arn:aws:iam:::role/r"):
        disconnect_mod.schedule_grace_check()

    kwargs = fake.create_schedule.call_args.kwargs
    assert kwargs["ActionAfterCompletion"] == "DELETE"
    assert kwargs["FlexibleTimeWindow"] == {"Mode": "OFF"}
    assert kwargs["ScheduleExpression"].startswith("at(")


def test_an_already_armed_grace_check_is_not_an_error(disconnect_mod):
    """One pending check is exactly what is wanted."""
    fake = mock.MagicMock()
    fake.create_schedule.side_effect = client_error("ConflictException")
    with mock.patch.object(disconnect_mod, "get_scheduler", lambda: fake), \
         mock.patch.object(disconnect_mod, "GRACE_TARGET_ARN", "arn:aws:lambda:::fn"), \
         mock.patch.object(disconnect_mod, "GRACE_ROLE_ARN", "arn:aws:iam:::role/r"):
        disconnect_mod.schedule_grace_check()   # must not raise


# --- grace check --------------------------------------------------------------


@pytest.fixture
def grace_mod():
    from lambdas import grace_check_handler
    return grace_check_handler


def test_grace_check_disables_polling_when_still_empty(grace_mod):
    with mock.patch.object(grace_mod, "get_connection_store", FakeConnectionStore), \
         mock.patch.object(grace_mod, "disable_polling") as disable:
        result = grace_mod.handler({}, None)

    assert result == {"connections": 0, "polling_disabled": True}
    disable.assert_called_once()


def test_grace_check_leaves_polling_on_if_someone_reconnected(grace_mod):
    """The case the grace period exists for: a page refresh."""
    store = FakeConnectionStore({("returning", "50_-5")})
    with mock.patch.object(grace_mod, "get_connection_store", lambda: store), \
         mock.patch.object(grace_mod, "disable_polling") as disable:
        result = grace_mod.handler({}, None)

    assert result == {"connections": 1, "polling_disabled": False}
    disable.assert_not_called()


def test_grace_check_ignores_its_event_payload(grace_mod):
    """The decision comes from the store now, not from what was true when the
    schedule was armed a minute ago."""
    with mock.patch.object(grace_mod, "get_connection_store", FakeConnectionStore), \
         mock.patch.object(grace_mod, "disable_polling"):
        for event in ({}, {"stale": "data"}, None or {}):
            assert grace_mod.handler(event, None)["polling_disabled"] is True


# --- poller -------------------------------------------------------------------


@pytest.fixture
def poller_mod():
    from lambdas import poller_handler
    return poller_handler


def test_poller_skips_entirely_when_nobody_is_connected(poller_mod):
    """The cost argument: no subscribers, no upstream call, no Kinesis write."""
    with mock.patch.object(poller_mod, "get_connection_store", FakeConnectionStore), \
         mock.patch.object(poller_mod, "get_poller") as build:
        assert poller_mod.handler({"source": "aws.events"}, None) == {
            "published": 0, "cells": 0
        }
    build.assert_not_called()   # no poller built, so no client, no upstream call


def test_poller_reports_what_it_published(poller_mod):
    store = FakeConnectionStore({("abc", "50_-5"), ("abc", "40_-75")})

    async def fake_poll():
        return 329

    with mock.patch.object(poller_mod, "get_connection_store", lambda: store), \
         mock.patch.object(poller_mod, "get_poller", lambda: mock.Mock(poll_once=fake_poll)):
        assert poller_mod.handler({}, None) == {"published": 329, "cells": 2}


# --- processor ----------------------------------------------------------------


@pytest.fixture
def processor_mod():
    from lambdas import processor_handler
    return processor_handler


def kinesis_event(*records):
    return {
        "Records": [
            {"kinesis": {"data": base64.b64encode(encode(r)).decode(),
                         "sequenceNumber": str(i)}}
            for i, r in enumerate(records)
        ]
    }


def test_processor_decodes_a_batch_and_broadcasts_it(processor_mod):
    store = FakeConnectionStore({("abc", "50_-5")})
    api = mock.MagicMock()
    broadcast = processor_mod.BroadcastToApiGateway(store, api=api)
    processor = processor_mod.Processor(FakePositionStore(), broadcast=broadcast)

    with mock.patch.object(processor_mod, "get_processor", lambda: processor):
        result = processor_mod.handler(kinesis_event(StreamRecord(PLANE, "50_-5")), None)

    # Empty list = whole batch succeeded, mapping checkpoints past all of it.
    assert result == {"batchItemFailures": []}
    assert api.post_to_connection.call_args.kwargs["ConnectionId"] == "abc"


def test_processor_sends_the_frame_shape_the_frontend_already_parses(processor_mod):
    import json
    message = json.loads(processor_mod.encode_message(StreamRecord(PLANE, "50_-5")))

    assert message["type"] == "aircraft"
    assert message["region_cell"] == "50_-5"
    assert message["state"]["icao24"] == "4caec4"
    assert message["state"]["registration"] == "EI-IRF"


def test_a_gone_connection_is_reaped_across_every_cell_it_held(processor_mod):
    """410 means the socket died without a $disconnect. Leaving its rows keeps
    the poller fetching sky for a client that no longer exists."""
    store = FakeConnectionStore({("dead", "50_-5"), ("dead", "50_0"), ("live", "50_-5")})
    api = mock.MagicMock()
    api.post_to_connection.side_effect = (
        lambda ConnectionId, Data: None if ConnectionId == "live"
        else (_ for _ in ()).throw(client_error("GoneException"))
    )
    broadcast = processor_mod.BroadcastToApiGateway(store, api=api)

    import asyncio
    delivered = asyncio.run(broadcast(StreamRecord(PLANE, "50_-5")))

    assert delivered == 1                       # only the live one counted
    assert store.rows == {("live", "50_-5")}    # both dead rows gone, not just this cell


def test_a_non_gone_error_propagates_rather_than_reaping_a_live_client(processor_mod):
    """Throttling or auth failure must not be papered over by deleting a
    subscriber's registration."""
    store = FakeConnectionStore({("abc", "50_-5")})
    api = mock.MagicMock()
    api.post_to_connection.side_effect = client_error("ThrottlingException")
    broadcast = processor_mod.BroadcastToApiGateway(store, api=api)

    import asyncio
    with pytest.raises(Exception):
        asyncio.run(broadcast(StreamRecord(PLANE, "50_-5")))

    assert store.rows == {("abc", "50_-5")}     # still registered


def test_a_failing_record_is_reported_not_raised(processor_mod):
    """Raising would fail the whole batch, which is what partial batch
    reporting exists to avoid."""
    failing = mock.MagicMock()

    async def boom(record):
        raise RuntimeError("downstream exploded")

    failing.process = boom
    with mock.patch.object(processor_mod, "get_processor", lambda: failing):
        result = processor_mod.handler(kinesis_event(StreamRecord(PLANE, "50_-5")), None)

    assert result == {"batchItemFailures": [{"itemIdentifier": "0"}]}


def test_only_the_bad_record_is_reported_not_its_neighbours(processor_mod):
    """The whole point: one malformed aircraft must not DLQ the good ones."""
    good = StreamRecord(PLANE, "50_-5")
    processed = []

    async def selective(record):
        if record.state.icao24 == "bad":
            raise ValueError("this one is broken")
        processed.append(record.state.icao24)
        return True

    from dataclasses import replace
    batch = kinesis_event(good, StreamRecord(replace(PLANE, icao24="bad"), "50_-5"), good)
    stub = mock.MagicMock()
    stub.process = selective

    with mock.patch.object(processor_mod, "get_processor", lambda: stub):
        result = processor_mod.handler(batch, None)

    assert result == {"batchItemFailures": [{"itemIdentifier": "1"}]}
    assert processed == ["4caec4", "4caec4"]   # the good records still ran


def test_processing_continues_past_a_failure(processor_mod):
    """A failure must not abandon the rest of the batch — the mapping replays
    from the lowest reported sequence number anyway, and stopping early would
    hide any later failures from the log."""
    seen = []

    async def fail_first(record):
        seen.append(record.state.icao24)
        if len(seen) == 1:
            raise RuntimeError("first one broke")
        return True

    stub = mock.MagicMock()
    stub.process = fail_first
    batch = kinesis_event(StreamRecord(PLANE, "50_-5"), StreamRecord(PLANE, "50_-5"))

    with mock.patch.object(processor_mod, "get_processor", lambda: stub):
        result = processor_mod.handler(batch, None)

    assert len(seen) == 2                                    # did not stop
    assert result == {"batchItemFailures": [{"itemIdentifier": "0"}]}


def test_an_undecodable_record_is_skipped_not_retried_forever(processor_mod):
    """A retry cannot fix malformed bytes; raising would replay the batch."""
    event = {"Records": [{"kinesis": {"data": base64.b64encode(b"garbage").decode(),
                                      "sequenceNumber": "1"}}]}
    with mock.patch.object(processor_mod, "get_processor", mock.MagicMock):
        # Not reported as a failure: retrying cannot fix malformed bytes, and
        # reporting it would replay the batch until the record aged out.
        assert processor_mod.handler(event, None) == {"batchItemFailures": []}


def test_an_empty_batch_is_fine(processor_mod):
    assert processor_mod.handler({"Records": []}, None) == {"batchItemFailures": []}


def test_broadcast_to_an_unwatched_cell_costs_no_api_calls(processor_mod):
    api = mock.MagicMock()
    broadcast = processor_mod.BroadcastToApiGateway(FakeConnectionStore(), api=api)

    import asyncio
    assert asyncio.run(broadcast(StreamRecord(PLANE, "0_0"))) == 0
    api.post_to_connection.assert_not_called()


# --- subscribe ----------------------------------------------------------------


@pytest.fixture
def subscribe_mod():
    from lambdas import subscribe_handler
    return subscribe_handler


def subscribe_event(body, connection_id="abc"):
    import json as _json
    return {
        "requestContext": {
            "connectionId": connection_id,
            "domainName": "xa4h68glz1.execute-api.us-east-1.amazonaws.com",
            "stage": "prod",
        },
        "body": _json.dumps(body) if not isinstance(body, str) else body,
    }


def test_subscribe_widens_coverage_to_the_viewport(subscribe_mod):
    """The deployed-panning bug: without this route a client keeps its one
    GeoIP cell forever."""
    store = FakeConnectionStore({("abc", "40_-75")})
    api = mock.MagicMock()
    positions = mock.MagicMock()
    positions.list_positions_in_cell.return_value = []
    with mock.patch.object(subscribe_mod, "get_connection_store", lambda: store), \
         mock.patch.object(subscribe_mod, "get_position_store", lambda: positions), \
         mock.patch.object(subscribe_mod, "get_management_api", lambda e="": api):
        result = subscribe_mod.handler(
            subscribe_event({"type": "subscribe", "bounds": [38.0, -76.0, 47.0, -68.0]}), None
        )

    assert result == {"statusCode": 200}
    assert len(store.rows) == DEFAULT_MAX_CELLS
    assert all(cid == "abc" for cid, _ in store.rows)
    # The client is told what it actually got.
    frame = json.loads(api.post_to_connection.call_args.kwargs["Data"])
    assert frame["type"] == "subscribed"
    assert len(frame["cells"]) == DEFAULT_MAX_CELLS


def test_subscribe_releases_cells_the_client_panned_away_from(subscribe_mod):
    """Otherwise every pan permanently adds requests to every poll cycle."""
    store = FakeConnectionStore({("abc", f"40_{lon}") for lon in (-80, -75, -70)})
    # Strictly inside 50_-5: longitude 0.0 would touch the next cell east.
    with mock.patch.object(subscribe_mod, "get_connection_store", lambda: store), \
         mock.patch.object(subscribe_mod, "get_position_store", lambda: mock.MagicMock(**{"list_positions_in_cell.return_value": []})), \
         mock.patch.object(subscribe_mod, "get_management_api", lambda e="": mock.MagicMock()):
        subscribe_mod.handler(
            subscribe_event({"type": "subscribe", "bounds": [51.0, -4.0, 52.0, -1.0]}), None
        )

    assert store.rows == {("abc", "50_-5")}


def test_a_move_inside_the_same_cell_costs_nothing(subscribe_mod):
    """The client re-sends on every map move because it cannot know where the
    boundaries are; the server is what makes that cheap."""
    store = FakeConnectionStore({("abc", "40_-75")})
    api = mock.MagicMock()
    with mock.patch.object(subscribe_mod, "get_connection_store", lambda: store), \
         mock.patch.object(subscribe_mod, "get_management_api", lambda e="": api):
        subscribe_mod.handler(subscribe_event({"type": "subscribe", "lat": 42.0, "lon": -72.0}), None)

    assert store.rows == {("abc", "40_-75")}
    api.post_to_connection.assert_not_called()


@pytest.mark.parametrize("body", [
    "not json", "{}", '{"type":"other"}', '{"type":"subscribe"}',
    '{"type":"subscribe","bounds":"nope"}', '{"type":"subscribe","lat":999,"lon":0}',
])
def test_a_bad_subscribe_leaves_the_subscription_untouched(subscribe_mod, body):
    store = FakeConnectionStore({("abc", "40_-75")})
    with mock.patch.object(subscribe_mod, "get_connection_store", lambda: store), \
         mock.patch.object(subscribe_mod, "get_position_store", lambda: mock.MagicMock(**{"list_positions_in_cell.return_value": []})), \
         mock.patch.object(subscribe_mod, "get_management_api", lambda e="": mock.MagicMock()):
        assert subscribe_mod.handler(subscribe_event(body), None) == {"statusCode": 200}

    assert store.rows == {("abc", "40_-75")}


def test_subscribe_without_a_connection_id_is_rejected(subscribe_mod):
    assert subscribe_mod.handler({"requestContext": {}, "body": "{}"}, None)["statusCode"] == 400


def test_the_endpoint_comes_from_the_request_not_the_environment(subscribe_mod):
    """A route handler already knows which API and stage invoked it."""
    expected = "https://xa4h68glz1.execute-api.us-east-1.amazonaws.com/prod"
    assert subscribe_mod.endpoint_from(subscribe_event({"type": "subscribe"})) == expected
    assert subscribe_mod.endpoint_from({"requestContext": {}}) == ""


def test_a_client_that_vanished_mid_subscribe_is_reaped(subscribe_mod):
    store = FakeConnectionStore({("abc", "40_-75")})
    api = mock.MagicMock()
    api.post_to_connection.side_effect = client_error("GoneException")
    with mock.patch.object(subscribe_mod, "get_connection_store", lambda: store), \
         mock.patch.object(subscribe_mod, "get_position_store", lambda: mock.MagicMock(**{"list_positions_in_cell.return_value": []})), \
         mock.patch.object(subscribe_mod, "get_management_api", lambda e="": api):
        subscribe_mod.handler(
            subscribe_event({"type": "subscribe", "bounds": [51.0, -4.0, 52.0, -1.0]}), None
        )

    assert store.rows == set()


def test_subscribe_sends_a_snapshot_of_newly_covered_cells(subscribe_mod):
    """The two-device problem: the state is already stored, so a joining client
    should not wait a poll cycle and then receive only the aircraft that moved."""
    store = FakeConnectionStore()
    api = mock.MagicMock()
    positions = mock.MagicMock()
    positions.list_positions_in_cell.return_value = [PLANE]

    with mock.patch.object(subscribe_mod, "get_connection_store", lambda: store), \
         mock.patch.object(subscribe_mod, "get_position_store", lambda: positions), \
         mock.patch.object(subscribe_mod, "get_management_api", lambda e="": api):
        subscribe_mod.handler(
            subscribe_event({"type": "subscribe", "lat": 51.5, "lon": -0.1}), None
        )

    frames = [json.loads(c.kwargs["Data"]) for c in api.post_to_connection.call_args_list]
    aircraft = [f for f in frames if f["type"] == "aircraft"]
    assert len(aircraft) == 1
    assert aircraft[0]["state"]["icao24"] == "4caec4"
    # Flagged so the client can tell a replay from a live update.
    assert aircraft[0]["snapshot"] is True


def test_the_snapshot_covers_only_cells_the_client_did_not_already_have(subscribe_mod):
    """Re-sending cells it was keeping is pure duplication."""
    store = FakeConnectionStore({("abc", "40_-75")})
    positions = mock.MagicMock()
    positions.list_positions_in_cell.return_value = []

    with mock.patch.object(subscribe_mod, "get_connection_store", lambda: store), \
         mock.patch.object(subscribe_mod, "get_position_store", lambda: positions), \
         mock.patch.object(subscribe_mod, "get_management_api", lambda e="": mock.MagicMock()):
        subscribe_mod.handler(
            subscribe_event({"type": "subscribe", "bounds": [38.0, -76.0, 42.0, -72.0]}), None
        )

    asked = {c.args[0] for c in positions.list_positions_in_cell.call_args_list}
    assert "40_-75" not in asked        # already held, not re-sent
    assert asked                        # but the genuinely new ones were


def test_unmappable_aircraft_are_left_out_of_the_snapshot(subscribe_mod):
    """A stored state with no position would be dropped by the frontend anyway."""
    store = FakeConnectionStore()
    api = mock.MagicMock()
    positions = mock.MagicMock()
    positions.list_positions_in_cell.return_value = [AircraftState(icao24="nopos")]

    with mock.patch.object(subscribe_mod, "get_connection_store", lambda: store), \
         mock.patch.object(subscribe_mod, "get_position_store", lambda: positions), \
         mock.patch.object(subscribe_mod, "get_management_api", lambda e="": api):
        subscribe_mod.handler(
            subscribe_event({"type": "subscribe", "lat": 51.5, "lon": -0.1}), None
        )

    frames = [json.loads(c.kwargs["Data"]) for c in api.post_to_connection.call_args_list]
    assert not [f for f in frames if f["type"] == "aircraft"]


def test_a_snapshot_failure_does_not_undo_the_subscription(subscribe_mod):
    """Best-effort: the client still populates the slow way."""
    store = FakeConnectionStore()
    api = mock.MagicMock()
    api.post_to_connection.side_effect = client_error("ThrottlingException")
    positions = mock.MagicMock()
    positions.list_positions_in_cell.return_value = [PLANE]

    with mock.patch.object(subscribe_mod, "get_connection_store", lambda: store), \
         mock.patch.object(subscribe_mod, "get_position_store", lambda: positions), \
         mock.patch.object(subscribe_mod, "get_management_api", lambda e="": api):
        result = subscribe_mod.handler(
            subscribe_event({"type": "subscribe", "lat": 51.5, "lon": -0.1}), None
        )

    assert result == {"statusCode": 200}
    assert store.rows == {("abc", "50_-5")}


def test_a_snapshot_failure_reports_that_the_snapshot_is_incomplete(subscribe_mod):
    """A throttled snapshot should be visible to the client instead of silent."""
    store = FakeConnectionStore()
    api = mock.MagicMock()
    positions = mock.MagicMock()
    positions.list_positions_in_cell.return_value = [PLANE]

    calls = []

    def post(ConnectionId, Data):
        frame = json.loads(Data)
        calls.append(frame)
        if frame["type"] == "aircraft":
            raise client_error("ThrottlingException")

    api.post_to_connection.side_effect = post

    with mock.patch.object(subscribe_mod, "get_connection_store", lambda: store), \
         mock.patch.object(subscribe_mod, "get_position_store", lambda: positions), \
         mock.patch.object(subscribe_mod, "get_management_api", lambda e="": api):
        subscribe_mod.handler(
            subscribe_event({"type": "subscribe", "lat": 51.5, "lon": -0.1}), None
        )

    statuses = [frame for frame in calls if frame["type"] == "snapshot_status"]
    assert statuses == [{"type": "snapshot_status", "sent": 0, "complete": False}]


# --- default route ------------------------------------------------------------


@pytest.fixture
def default_mod():
    from lambdas import default_handler
    return default_handler


def test_default_route_explains_unknown_messages(default_mod):
    api = mock.MagicMock()
    with mock.patch.object(default_mod, "get_management_api", lambda e="": api):
        result = default_mod.handler(subscribe_event({"type": "bogus"}), None)

    assert result == {"statusCode": 200}
    frame = json.loads(api.post_to_connection.call_args.kwargs["Data"])
    assert frame["type"] == "error"
    assert frame["code"] == "unknown_message_type"


def test_default_route_requires_a_connection_id(default_mod):
    assert default_mod.handler({"requestContext": {}}, None)["statusCode"] == 400
