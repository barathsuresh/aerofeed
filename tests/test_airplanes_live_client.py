"""Tests for the airplanes.live client.

The fixture in tests/fixtures/ is a real captured /v2/point response, trimmed
to seven records chosen to cover every shape the parser has to survive. Real,
not synthetic, because the cases that break parsers here are exactly the ones
nobody thinks to invent — `flight` absent entirely, `track` missing on surface
aircraft, `alt_baro` carrying a string. One record is annotated in
FIXTURE_NOTES as modified; everything else is verbatim wire data.

No network: the session is stubbed.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import requests

from core.airplanes_live_client import (
    FEET_PER_MIN_TO_M_S,
    FEET_TO_METRES,
    KNOTS_TO_M_S,
    MAX_RADIUS_NM,
    AirplanesLiveClient,
    AirplanesLiveError,
    parse_now,
    parse_state,
)
from core.models import (
    POSITION_ESTIMATED,
    POSITION_LAST_KNOWN,
    POSITION_LIVE,
)

FIXTURE = Path(__file__).parent / "fixtures" / "airplanes_live_point.json"

# The capture window contained no dot-prefixed callsign, so that one field was
# grafted onto a real record to pin the documented ".N954JB " form. Everything
# else, including every hex, is untouched wire data.
FIXTURE_NOTES = {"a73ceb": "flight field replaced with '.N954JB '"}


@pytest.fixture
def payload():
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def records(payload):
    return {record["hex"]: record for record in payload["ac"]}


class StubResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class StubSession:
    """Records the URLs asked for and replays a canned response."""

    def __init__(self, response):
        self._response = response
        self.urls: list[str] = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        if isinstance(self._response, requests.RequestException):
            raise self._response
        return self._response


# --- field mapping -----------------------------------------------------------


def test_airborne_record_maps_every_field(records):
    state = parse_state(records["a3aca6"])

    assert state.icao24 == "a3aca6"
    assert state.callsign == "FFT3511"  # wire value is space-padded
    assert state.latitude == pytest.approx(40.527649)
    assert state.longitude == pytest.approx(-75.070312)
    assert state.true_track == pytest.approx(230.88)
    assert state.squawk == "2412"
    assert state.on_ground is False


def test_altitudes_and_speeds_are_converted_to_metric(records):
    """The wire is feet and knots; AircraftState is documented in metres and m/s.

    Pinned with literals rather than the constants alone so a change to the
    conversion has to be deliberate — delta.ALTITUDE_EPSILON_M is metres, and
    passing feet through would silently make the delta filter 3x as sensitive.
    """
    state = parse_state(records["a3aca6"])

    assert state.baro_altitude == pytest.approx(36000 * FEET_TO_METRES)
    assert state.baro_altitude == pytest.approx(10972.8)
    assert state.geo_altitude == pytest.approx(37425 * FEET_TO_METRES)
    assert state.velocity == pytest.approx(483.4 * KNOTS_TO_M_S)
    assert state.velocity == pytest.approx(248.68, abs=0.01)
    # 64 ft/min climb is a third of a metre per second, not 64 of anything.
    assert state.vertical_rate == pytest.approx(0.325, abs=0.001)


def test_descending_aircraft_keeps_its_sign(records):
    assert parse_state(records["ae5fa7"]).vertical_rate == pytest.approx(-3.912, abs=0.001)


# --- vertical rate: two non-overlapping sources ------------------------------


def test_geom_rate_is_used_when_baro_rate_is_absent():
    """50 of 272 records in the capture had geom_rate and no baro_rate.

    Without the fallback that whole group reports level flight while climbing.
    """
    state = parse_state({"hex": "abc123", "geom_rate": 192})

    assert state.vertical_rate == pytest.approx(192 * FEET_PER_MIN_TO_M_S)
    assert state.vertical_rate == pytest.approx(0.975, abs=0.001)


def test_baro_rate_wins_when_both_are_present():
    """Matches AircraftState.altitude's baro-first preference, so an
    aircraft's altitude and its rate of change agree on a source."""
    state = parse_state({"hex": "abc123", "baro_rate": 64, "geom_rate": 960})

    assert state.vertical_rate == pytest.approx(64 * FEET_PER_MIN_TO_M_S)


def test_neither_rate_is_none_not_zero():
    """44 of 272 had neither. Zero would assert level flight we cannot see."""
    assert parse_state({"hex": "abc123"}).vertical_rate is None


def test_zero_baro_rate_is_not_skipped_for_geom_rate():
    """A real 0 ft/min must win over the fallback, not fall through it."""
    state = parse_state({"hex": "abc123", "baro_rate": 0, "geom_rate": 960})

    assert state.vertical_rate == 0.0


# --- timestamps derived from the envelope ------------------------------------


def test_now_is_read_as_milliseconds(payload):
    """Documented as seconds, actually milliseconds on the wire.

    Treating the raw value as seconds would date every position to the year
    58500, so the unit is derived from magnitude, not from the docs.
    """
    assert payload["now"] > 1e11  # the fixture really is in millis
    now_s = parse_now(payload)

    assert 1_700_000_000 < now_s < 2_000_000_000  # a plausible unix second


def test_now_accepts_seconds_too():
    """A provider fix must not silently shift every timestamp by 1000x."""
    assert parse_now({"now": 1785516434}) == pytest.approx(1785516434)


@pytest.mark.parametrize("body", [{}, {"now": None}, {"now": 0}, {"now": "soon"}, [], None])
def test_missing_or_unusable_now_is_none(body):
    assert parse_now(body) is None


def test_ages_become_absolute_timestamps(records):
    """`seen_pos`/`seen` are ages before `now`; the model stores absolute times."""
    now_s = 1785516434.0
    state = parse_state(records["a3aca6"], now_s)

    assert state.time_position == int(now_s - records["a3aca6"]["seen_pos"])
    assert state.last_contact == int(now_s - records["a3aca6"]["seen"])
    assert state.time_position <= int(now_s)


def test_a_stale_position_timestamps_further_back():
    now_s = 1785516434.0
    fresh = parse_state({"hex": "aaa111", "lat": 1.0, "lon": 2.0, "seen_pos": 0.1}, now_s)
    stale = parse_state({"hex": "bbb222", "lat": 1.0, "lon": 2.0, "seen_pos": 55.0}, now_s)

    assert stale.time_position < fresh.time_position
    assert fresh.time_position - stale.time_position == pytest.approx(55, abs=1)


def test_no_position_means_no_position_timestamp():
    """time_position dates a *position*. Without one there is nothing to date,
    even though `seen_pos` is present."""
    state = parse_state({"hex": "abc123", "seen_pos": 0.1, "seen": 0.1}, 1785516434.0)

    assert state.time_position is None
    assert state.last_contact is not None  # we were still heard from


def test_timestamps_stay_none_without_a_now(records):
    """No anchor means no timestamp. Falling back to the local clock would
    fold our own skew and the request's flight time into every reading."""
    state = parse_state(records["a3aca6"])

    assert state.time_position is None
    assert state.last_contact is None
    # Everything else parses identically with or without it.
    assert state.callsign == parse_state(records["a3aca6"], 1785516434.0).callsign


def test_get_states_stamps_every_record_from_one_reading(payload):
    """One `now` for the batch: aircraft in a single snapshot must not
    disagree about when the snapshot happened."""
    session = StubSession(StubResponse(payload))

    states = AirplanesLiveClient(session=session).get_states(40.7, -74.0, 50)

    stamped = [s for s in states if s.last_contact is not None]
    assert stamped, "the fixture carries `now` and `seen`, so these must be set"
    # Ages in the fixture are all sub-second to a few seconds apart.
    assert max(s.last_contact for s in stamped) - min(s.last_contact for s in stamped) < 300


# --- the "ground" string -----------------------------------------------------


def test_ground_string_becomes_on_ground_not_an_altitude(records):
    """alt_baro == "ground" is a normal code path: 47 of 272 records in the
    capture this fixture came from carried it."""
    state = parse_state(records["a1c82c"])

    assert state.on_ground is True
    assert state.baro_altitude is None
    # Nothing downstream should ever see the raw string.
    assert not isinstance(state.baro_altitude, str)
    assert state.altitude is None


def test_numeric_altitude_means_airborne(records):
    assert parse_state(records["a8776b"]).on_ground is False


def test_absent_altitude_is_unknown_not_airborne():
    """Absent must not become on_ground=False, which would invent a takeoff."""
    state = parse_state({"hex": "abc123", "lat": 1.0, "lon": 2.0})

    assert state.on_ground is None
    assert state.baro_altitude is None


@pytest.mark.parametrize("hex_id", ["a1c82c", "a4f622"])
def test_ground_states_survive_the_delta_comparison(records, hex_id):
    """The whole reason "ground" is mapped rather than passed through.

    A ground state and an airborne state must be comparable without a
    string-vs-number TypeError, on either side of the comparison.
    """
    from core.delta import has_meaningfully_changed

    ground = parse_state(records[hex_id])
    airborne = replace(parse_state(records["a3aca6"]), icao24=ground.icao24)

    assert has_meaningfully_changed(ground, airborne) is True
    assert has_meaningfully_changed(airborne, ground) is True
    assert has_meaningfully_changed(ground, ground) is False


# --- callsigns ---------------------------------------------------------------


def test_leading_dots_and_padding_are_stripped(records):
    assert FIXTURE_NOTES["a73ceb"]  # this record's flight field is grafted
    assert parse_state(records["a73ceb"]).callsign == "N954JB"


def test_absent_flight_field_gives_none_not_empty_string(records):
    """`flight` is missing outright on some records, not merely blank."""
    assert "flight" not in records["~2ae680"]
    assert parse_state(records["~2ae680"]).callsign is None


@pytest.mark.parametrize(
    "flight,expected",
    [
        ("DLH441  ", "DLH441"),
        (".N954JB ", "N954JB"),
        ("..ABC123", "ABC123"),
        ("        ", None),
        ("....", None),
        ("", None),
        (None, None),
    ],
)
def test_callsign_normalisation(flight, expected):
    assert parse_state({"hex": "abc123", "flight": flight}).callsign == expected


# --- missing and malformed fields --------------------------------------------


def test_surface_aircraft_falls_back_to_heading_when_track_is_absent(records):
    """43 of 47 ground records in the capture had no `track`."""
    record = records["a1c82c"]
    assert "track" not in record

    assert parse_state(record).true_track == pytest.approx(329.06)


def test_no_direction_at_all_is_none_not_zero(records):
    """Zero is a real bearing (due north); absent must stay absent."""
    record = records["a4f622"]
    assert not {"track", "true_heading", "mag_heading"} & record.keys()

    assert parse_state(record).true_track is None


def test_missing_optional_fields_are_none_not_errors(records):
    state = parse_state(records["a4f622"])

    assert state.squawk is None
    assert state.geo_altitude is None
    assert state.vertical_rate is None
    assert state.icao24 == "a4f622"  # still usable


def test_only_hex_is_required():
    state = parse_state({"hex": "ABC123"})

    assert state.icao24 == "abc123"  # case normalised
    assert state.callsign is None
    assert state.has_position is False


@pytest.mark.parametrize("record", [{}, {"hex": None}, {"hex": "  "}, [], "hex", None, 42])
def test_records_without_a_usable_identity_are_dropped(record):
    assert parse_state(record) is None


@pytest.mark.parametrize("garbage", ["abc", {}, [], True, False])
def test_garbage_in_a_numeric_field_is_none_not_an_exception(garbage):
    state = parse_state({"hex": "abc123", "gs": garbage, "track": garbage, "lat": garbage})

    assert state.velocity is None
    assert state.true_track is None
    assert state.latitude is None


def test_zero_values_are_preserved_not_treated_as_missing():
    """A stationary aircraft on a northerly heading reports real zeroes."""
    state = parse_state({"hex": "abc123", "gs": 0, "track": 0, "baro_rate": 0})

    assert state.velocity == 0.0
    assert state.true_track == 0.0
    assert state.vertical_rate == 0.0


# --- get_states --------------------------------------------------------------


def test_get_states_parses_the_whole_fixture(payload):
    session = StubSession(StubResponse(payload))
    client = AirplanesLiveClient(session=session)

    states = client.get_states(40.7, -74.0, 50)

    assert len(states) == len(payload["ac"])
    assert {s.icao24 for s in states} == {r["hex"].lower() for r in payload["ac"]}
    # Every state is addressable and metric — the contract downstream relies on.
    assert all(s.icao24 for s in states)
    assert all(s.baro_altitude is None or isinstance(s.baro_altitude, float) for s in states)


def test_get_states_builds_the_point_radius_url(payload):
    session = StubSession(StubResponse(payload))

    AirplanesLiveClient(session=session).get_states(40.7, -74.0, 123.5)

    assert session.urls == ["https://api.airplanes.live/v2/point/40.7/-74.0/123.5"]


def test_radius_is_clamped_to_the_provider_limit(payload):
    """The API rejects anything over 250 nm; a 400 would lose the whole poll."""
    session = StubSession(StubResponse(payload))

    AirplanesLiveClient(session=session).get_states(0.0, 0.0, 9999)

    assert session.urls[0].endswith(f"/{float(MAX_RADIUS_NM)}")


def test_empty_sky_is_an_empty_list_not_an_error():
    for body in ({"ac": [], "total": 0}, {"ac": None}, {}, []):
        session = StubSession(StubResponse(body))
        assert AirplanesLiveClient(session=session).get_states(0.0, 0.0, 10) == []


def test_unidentifiable_records_are_dropped_without_losing_the_rest():
    body = {"ac": [{"hex": "aaa111"}, {"no_hex": True}, "garbage", {"hex": "bbb222"}]}
    session = StubSession(StubResponse(body))

    states = AirplanesLiveClient(session=session).get_states(0.0, 0.0, 10)

    assert [s.icao24 for s in states] == ["aaa111", "bbb222"]


def test_transport_failure_raises_airplanes_live_error():
    session = StubSession(requests.ConnectionError("no route to host"))

    with pytest.raises(AirplanesLiveError, match="states request failed"):
        AirplanesLiveClient(session=session).get_states(0.0, 0.0, 10)


def test_non_2xx_raises_airplanes_live_error():
    session = StubSession(StubResponse({}, status_code=429))

    with pytest.raises(AirplanesLiveError, match="states request failed"):
        AirplanesLiveClient(session=session).get_states(0.0, 0.0, 10)


def test_unparseable_body_raises_airplanes_live_error():
    session = StubSession(StubResponse(ValueError("not json")))

    with pytest.raises(AirplanesLiveError, match="not valid JSON"):
        AirplanesLiveClient(session=session).get_states(0.0, 0.0, 10)


def test_client_needs_no_credentials():
    """The migration's headline: nothing to configure, nothing to leak."""
    AirplanesLiveClient()


# --- identity from the provider's database -----------------------------------


GUIDE_EXAMPLE = {
    "hex": "45211e", "type": "mode_s", "flight": "CFG846 ", "r": "LZ-LAJ",
    "t": "A320", "desc": "AIRBUS A-320", "alt_baro": 37000, "gs": 496,
    "track": 113.55, "baro_rate": 0, "geom_rate": 0, "squawk": "7665",
    "emergency": "none", "category": "A3", "rr_lat": 40.7, "rr_lon": 39.3,
    "lastPosition": {"lat": 43.261414, "lon": 29.636404, "seen_pos": 3061.406},
    "seen": 0.5,
}
"""The API guide's own example: a Mode S aircraft with no live position."""


def test_identity_fields_are_mapped():
    state = parse_state(GUIDE_EXAMPLE)

    assert state.registration == "LZ-LAJ"
    assert state.aircraft_type == "A320"
    assert state.description == "AIRBUS A-320"
    assert state.category == "A3"
    assert state.data_source == "mode_s"


def test_identity_is_none_when_the_aircraft_is_not_in_the_database():
    state = parse_state({"hex": "abc123"})

    assert state.registration is None
    assert state.aircraft_type is None
    assert state.description is None


@pytest.mark.parametrize(
    "flags,expected",
    [(1, True), (3, True), (9, True), (0, False), (2, False), (8, False), (None, None)],
)
def test_military_is_read_out_of_the_dbflags_bitfield(flags, expected):
    """military is bit 0; interesting/PIA/LADD share the field and must not
    be mistaken for it."""
    record = {"hex": "abc123"} if flags is None else {"hex": "abc123", "dbFlags": flags}

    assert parse_state(record).military is expected


@pytest.mark.parametrize("garbage", ["mil", {}, [], True])
def test_garbage_dbflags_is_unknown_not_false(garbage):
    """Absent or unreadable means unknown. False would assert 'civilian'."""
    assert parse_state({"hex": "abc123", "dbFlags": garbage}).military is None


# --- position rescue: the mode_s case ----------------------------------------


def test_live_position_is_preferred_and_labelled():
    state = parse_state({"hex": "abc123", "lat": 40.0, "lon": -74.0, "seen_pos": 0.3}, 1000.0)

    assert (state.latitude, state.longitude) == (40.0, -74.0)
    assert state.position_source == POSITION_LIVE
    assert state.has_position is True


def test_last_known_position_is_used_when_there_is_no_live_fix():
    """The API guide's example record: transmitting now, last fix 51 min ago.

    Before this fallback the aircraft was parsed, stored, broadcast — and then
    silently dropped by the frontend for having no coordinates.
    """
    now_s = 1695420989.961
    state = parse_state(GUIDE_EXAMPLE, now_s)

    assert state.has_position is True
    assert state.latitude == pytest.approx(43.261414)
    assert state.longitude == pytest.approx(29.636404)
    assert state.position_source == POSITION_LAST_KNOWN
    # Timestamp comes from lastPosition's own age, not the record's `seen`.
    assert state.time_position == int(now_s - 3061.406)


def test_last_known_is_timestamped_older_than_last_contact():
    """The whole point: still being heard, but not where it says it is."""
    state = parse_state(GUIDE_EXAMPLE, 1695420989.961)

    assert state.last_contact - state.time_position == pytest.approx(3061, abs=2)


def test_estimated_position_is_the_last_resort():
    record = {k: v for k, v in GUIDE_EXAMPLE.items() if k != "lastPosition"}
    state = parse_state(record, 1695420989.961)

    assert (state.latitude, state.longitude) == (40.7, 39.3)
    assert state.position_source == POSITION_ESTIMATED
    # An estimate was never reported at a moment; giving it a time would make
    # a guess indistinguishable from a measurement.
    assert state.time_position is None


def test_no_position_from_any_source_stays_unmappable():
    state = parse_state({"hex": "abc123", "alt_baro": 37000})

    assert state.has_position is False
    assert state.position_source is None


def test_a_half_present_position_is_not_used():
    """lat without lon is not a position; it must fall through, not crash."""
    state = parse_state({"hex": "abc123", "lat": 40.0, "rr_lat": 1.0, "rr_lon": 2.0})

    assert (state.latitude, state.longitude) == (1.0, 2.0)
    assert state.position_source == POSITION_ESTIMATED


@pytest.mark.parametrize("last", ["ground", [], 42, None, {}, {"lat": 1.0}])
def test_malformed_lastposition_falls_through_without_raising(last):
    state = parse_state({"hex": "abc123", "lastPosition": last, "rr_lat": 5.0, "rr_lon": 6.0})

    assert state.position_source == POSITION_ESTIMATED


def test_every_real_record_in_the_fixture_is_a_live_fix(records):
    """Sanity on the happy path: the rescue chain must not hijack normal data."""
    for record in records.values():
        assert parse_state(record).position_source == POSITION_LIVE


# --- emergency ---------------------------------------------------------------


@pytest.mark.parametrize("squawk", ["7500", "7600", "7700"])
def test_emergency_squawks_are_flagged_even_when_the_adsb_field_says_none(squawk):
    """Two independent channels; an aircraft may set either."""
    state = parse_state({"hex": "abc123", "squawk": squawk, "emergency": "none"})

    assert state.is_emergency is True


@pytest.mark.parametrize("declared", ["general", "lifeguard", "minfuel", "nordo", "downed"])
def test_declared_emergency_is_flagged_on_a_normal_squawk(declared):
    state = parse_state({"hex": "abc123", "squawk": "1200", "emergency": declared})

    assert state.is_emergency is True


@pytest.mark.parametrize("squawk", ["1200", "7665", "7000", None, "7501", "750"])
def test_ordinary_traffic_is_not_an_emergency(squawk):
    """7665 is the guide example's squawk — close to 7700, not an emergency."""
    state = parse_state({"hex": "abc123", "squawk": squawk, "emergency": "none"})

    assert state.is_emergency is False


def test_absent_emergency_field_is_not_an_emergency():
    assert parse_state({"hex": "abc123"}).is_emergency is False
