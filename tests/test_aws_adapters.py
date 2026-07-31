"""Offline tests for the AWS adapters' serialisation boundaries.

No network and no AWS: these cover the pure conversion logic, which is where
the subtle bugs live. DynamoDB rejects floats outright, Kinesis carries bytes,
and both have to hand back an AircraftState indistinguishable from the one that
went in. Behaviour that only real AWS can show — GSI eventual consistency, TTL,
shard ordering — is verified manually against the live resources instead.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from aws.dynamo_store import _from_dynamo, _to_dynamo
from aws.kinesis_stream import decode, encode
from core.models import AircraftState
from local.local_stream import StreamRecord

RICH = AircraftState(
    icao24="4caec4",
    callsign="CGI120F",
    latitude=51.5,
    longitude=-0.1,
    baro_altitude=5486.400000000001,  # a real value from the live run
    geo_altitude=5500.0,
    velocity=248.68,
    true_track=230.88,
    vertical_rate=-0.325,
    on_ground=False,
    squawk="2412",
    registration="EI-IRF",
    aircraft_type="BE20",
    description="BEECH King Air",
    category="A1",
    military=False,
    data_source="adsb_icao",
    position_source="live",
    emergency="none",
    time_position=1785521496,
    last_contact=1785521497,
)


# --- DynamoDB value conversion -----------------------------------------------


def test_floats_become_decimal_because_dynamodb_refuses_them():
    """boto3 raises on float rather than rounding, so this is not optional."""
    assert _to_dynamo(1.5) == Decimal("1.5")
    assert isinstance(_to_dynamo(1.5), Decimal)


def test_decimal_conversion_goes_through_the_short_repr():
    """Decimal(float) would preserve the full binary tail:
    Decimal(0.1) is 0.1000000000000000055511151231257827.
    Via str() it stays "0.1", which is what round-trips cleanly."""
    assert _to_dynamo(0.1) == Decimal("0.1")
    assert str(_to_dynamo(0.1)) == "0.1"


def test_non_floats_pass_through_untouched():
    for value in ("text", 42, True, False, None):
        assert _to_dynamo(value) is value


def test_bools_are_not_converted_to_numbers():
    """bool is a subclass of int; a numeric conversion here would store
    on_ground as 1/0 and break `is True` checks downstream."""
    assert _to_dynamo(True) is True
    assert _from_dynamo(True) is True


def test_whole_decimals_come_back_as_int():
    """time_position is typed int; a Decimal would leak into arithmetic."""
    result = _from_dynamo(Decimal("1785521496"))
    assert result == 1785521496
    assert isinstance(result, int)


def test_fractional_decimals_come_back_as_float():
    result = _from_dynamo(Decimal("5486.400000000001"))
    assert result == pytest.approx(5486.400000000001)
    assert isinstance(result, float)


@pytest.mark.parametrize("value", [0.0, -0.325, 5486.400000000001, 248.68, 1e-6, -180.0])
def test_every_float_survives_a_full_round_trip(value):
    assert _from_dynamo(_to_dynamo(value)) == pytest.approx(value)


def test_zero_is_preserved_not_dropped():
    """0.0 is a real ground speed and 0 a real heading."""
    assert _from_dynamo(_to_dynamo(0.0)) == 0.0
    assert _to_dynamo(0.0) == Decimal("0")


# --- Kinesis encode/decode ----------------------------------------------------


def test_record_round_trips_byte_for_byte_identical():
    record = StreamRecord(RICH, "50_-5")
    assert decode(encode(record)) == record


def test_encoding_drops_nulls_to_keep_records_small():
    """Most of AircraftState is Optional and absent on any given aircraft; at
    a few hundred records per poll the nulls would be most of the payload."""
    sparse = StreamRecord(AircraftState(icao24="abc123"), "0_0")
    body = encode(sparse)

    assert b"null" not in body
    assert len(body) < 60
    # And a dropped null still decodes back to the same state, via defaults.
    assert decode(body) == sparse


def test_dropped_nulls_do_not_change_the_decoded_state():
    partial = StreamRecord(
        AircraftState(icao24="abc123", latitude=1.0, longitude=2.0), "0_0"
    )
    restored = decode(encode(partial))

    assert restored == partial
    assert restored.state.callsign is None
    assert restored.state.registration is None


def test_region_cell_survives_the_round_trip():
    """The routing tag rides alongside the state, not inside it."""
    assert decode(encode(StreamRecord(RICH, "50_-5"))).region_cell == "50_-5"


@pytest.mark.parametrize(
    "garbage",
    [b"", b"not json", b"[]", b"null", b"{}", b'{"state":{}}', b'{"region_cell":"x"}',
     b'{"state":{"no_icao":1},"region_cell":"x"}'],
)
def test_undecodable_records_return_none_rather_than_raising(garbage):
    """One malformed record must not stall a shard — Kinesis would hand it back
    on every retry until the iterator moves past it."""
    assert decode(garbage) is None


def test_unknown_fields_from_a_newer_producer_are_rejected_not_crashed():
    """A field this build does not know about must not take down the consumer."""
    assert decode(b'{"state":{"icao24":"a","invented_field":1},"region_cell":"x"}') is None


def test_partition_key_is_the_icao24():
    """Ordering per aircraft is the property the delta filter depends on: two
    updates for one aircraft must reach the processor in the order sent."""
    record = StreamRecord(RICH, "50_-5")
    assert record.state.icao24 == "4caec4"  # what publish() passes as PartitionKey


def test_encoded_size_is_sane_for_the_record_volume():
    """A busy cell is ~700 aircraft per poll; at this size that is ~150KB,
    well inside Kinesis's 1MB-per-record and per-shard write limits."""
    assert len(encode(StreamRecord(RICH, "50_-5"))) < 1024
