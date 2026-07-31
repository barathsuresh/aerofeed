"""Tests for the change-detection throttle."""

from __future__ import annotations

from dataclasses import replace

import pytest

from core.delta import ALTITUDE_EPSILON_M, POSITION_EPSILON_DEG, has_meaningfully_changed
from core.models import AircraftState

BASE = AircraftState(
    icao24="a1b2c3",
    callsign="DLH441",
    latitude=40.0,
    longitude=-75.0,
    baro_altitude=10000.0,
    on_ground=False,
)


def state(**overrides) -> AircraftState:
    """BASE with fields replaced — keeps each test to the field it's about."""
    return replace(BASE, **overrides)


def test_first_sighting_is_always_a_change():
    assert has_meaningfully_changed(None, BASE) is True


def test_identical_state_is_not_a_change():
    assert has_meaningfully_changed(BASE, state()) is False


def test_sub_threshold_drift_is_not_a_change():
    # Jitter under every epsilon at once: nothing here reaches a client's screen.
    new = state(
        latitude=BASE.latitude + POSITION_EPSILON_DEG / 2,
        longitude=BASE.longitude - POSITION_EPSILON_DEG / 2,
        baro_altitude=BASE.baro_altitude + ALTITUDE_EPSILON_M / 2,
    )
    assert has_meaningfully_changed(BASE, new) is False


@pytest.mark.parametrize("field", ["latitude", "longitude"])
def test_position_change_beyond_epsilon(field):
    new = state(**{field: getattr(BASE, field) + POSITION_EPSILON_DEG * 2})
    assert has_meaningfully_changed(BASE, new) is True


def test_position_exactly_at_epsilon_is_not_a_change():
    # Threshold is strict ">"; pinned so a later refactor can't flip it.
    new = state(latitude=BASE.latitude + POSITION_EPSILON_DEG)
    assert has_meaningfully_changed(BASE, new) is False


def test_altitude_change_beyond_epsilon():
    new = state(baro_altitude=BASE.baro_altitude + ALTITUDE_EPSILON_M * 2)
    assert has_meaningfully_changed(BASE, new) is True


def test_altitude_change_below_epsilon():
    new = state(baro_altitude=BASE.baro_altitude + ALTITUDE_EPSILON_M - 1)
    assert has_meaningfully_changed(BASE, new) is False


@pytest.mark.parametrize("was_on_ground", [True, False])
def test_on_ground_flip_is_a_change(was_on_ground):
    # Flag flips even with the aircraft otherwise stationary — takeoff/landing.
    old = state(on_ground=was_on_ground)
    new = state(on_ground=not was_on_ground)
    assert has_meaningfully_changed(old, new) is True


def test_null_fields_do_not_raise_and_report_no_change():
    blank = AircraftState(icao24="a1b2c3")
    assert has_meaningfully_changed(blank, AircraftState(icao24="a1b2c3")) is False


@pytest.mark.parametrize("field", ["latitude", "longitude", "baro_altitude", "on_ground"])
def test_field_appearing_or_disappearing_is_a_change(field):
    # Transponder dropping out, or a first fix, is real news for the client.
    missing = state(**{field: None})
    assert has_meaningfully_changed(missing, BASE) is True
    assert has_meaningfully_changed(BASE, missing) is True


def test_geo_altitude_fallback_is_used_when_baro_absent():
    old = state(baro_altitude=None, geo_altitude=10000.0)
    new = state(baro_altitude=None, geo_altitude=10000.0 + ALTITUDE_EPSILON_M * 2)
    assert has_meaningfully_changed(old, new) is True


def test_velocity_and_callsign_changes_are_ignored():
    # Velocity moves nearly every poll; filtering on it would defeat the throttle.
    new = state(velocity=250.0, callsign="DLH999", squawk="7000")
    assert has_meaningfully_changed(BASE, new) is False


# --- emergency and position provenance ---------------------------------------


@pytest.mark.parametrize("squawk", ["7500", "7600", "7700"])
def test_declaring_an_emergency_is_always_a_change(squawk):
    """The one update that must never be filtered as insignificant."""
    calm = state(squawk="1200")
    alarm = state(squawk=squawk)

    assert has_meaningfully_changed(calm, alarm) is True
    assert has_meaningfully_changed(alarm, calm) is True  # and standing down


def test_declared_emergency_field_also_counts():
    calm = state(emergency="none")
    alarm = state(emergency="lifeguard")

    assert has_meaningfully_changed(calm, alarm) is True


def test_an_ordinary_squawk_change_is_still_ignored():
    """Squawk churns on handoff between controllers; only emergencies matter."""
    assert has_meaningfully_changed(state(squawk="1200"), state(squawk="2412")) is False


def test_position_going_stale_is_a_change():
    """A stale position stops moving by definition, so no other check here
    would ever catch the transition."""
    live = state(position_source="live")
    gone = state(position_source="last_known")

    assert has_meaningfully_changed(live, gone) is True
    assert has_meaningfully_changed(gone, live) is True


def test_identity_fields_do_not_trigger_broadcasts():
    """Database lookups, not telemetry — they do not change in flight, and a
    provider backfill must not spam every client."""
    old = state(registration=None, aircraft_type=None, description=None)
    new = state(registration="N954JB", aircraft_type="A320", description="AIRBUS A-320")

    assert has_meaningfully_changed(old, new) is False
