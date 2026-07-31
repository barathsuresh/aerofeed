"""Decide whether a new state vector is worth pushing to subscribers.

OpenSky re-reports every aircraft in the box each poll whether or not anything
happened. This module is the throttle every fan-out path gates on. Thresholds
are constants so they can be tuned against real traffic — starting points, not
truths.
"""

from __future__ import annotations

from typing import Optional

from .models import AircraftState

# ~1.1 km of latitude; below this an airliner has barely moved a map pixel.
POSITION_EPSILON_DEG = 0.01

# Under two seconds of climb for a departing jet, and above the reporting noise
# floor (barometric altitude quantises to 25 ft).
ALTITUDE_EPSILON_M = 50.0


def _crossed_threshold(
    old_value: Optional[float], new_value: Optional[float], epsilon: float
) -> bool:
    """Compare two optional numbers, counting appear/disappear as a change.

    Value -> None (transponder dropped) and None -> value (first fix) are real
    events. None -> None is not: still no data.
    """
    if old_value is None and new_value is None:
        return False
    if old_value is None or new_value is None:
        return True
    return abs(new_value - old_value) > epsilon


def has_meaningfully_changed(old: Optional[AircraftState], new: AircraftState) -> bool:
    """Report whether `new` differs from `old` enough to notify clients.

    Meaningful means any of: position moved more than POSITION_EPSILON_DEG in
    lat or lon; altitude changed more than ALTITUDE_EPSILON_M; on_ground flipped.

    Args:
        old: Last state pushed for this aircraft, or None on first sighting.
        new: Freshly polled state.

    Returns:
        True when the update should be propagated.

    Note:
        Ignores callsign, squawk, velocity and vertical rate. Velocity changes
        on nearly every poll and would defeat the filter. Add a field here only
        when a client actually renders it.
    """
    # First sighting — client has nothing at all. Checked first so nothing below
    # dereferences a None.
    if old is None:
        return True

    # Before position: a landing rollout can flip the flag while moving less
    # than one epsilon, and it's the most interesting event in a track.
    if old.on_ground != new.on_ground:
        return True

    if _crossed_threshold(old.latitude, new.latitude, POSITION_EPSILON_DEG):
        return True
    if _crossed_threshold(old.longitude, new.longitude, POSITION_EPSILON_DEG):
        return True

    # ponytail: .altitude falls back baro -> GNSS, so a state that switches
    # source can report a spurious change (the two differ by tens of metres).
    # Track per-source if that shows up in real traffic.
    if _crossed_threshold(old.altitude, new.altitude, ALTITUDE_EPSILON_M):
        return True

    return False
