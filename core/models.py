"""Core domain models. Data only — no persistence, transport or cloud concerns.

Upstream providers null or omit every field except icao24, so almost everything
here is Optional.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

# How a state's coordinates were obtained, worst case last.
POSITION_LIVE = "live"  # a current fix from the aircraft
POSITION_LAST_KNOWN = "last_known"  # real fix, but stale (provider's lastPosition)
POSITION_ESTIMATED = "estimated"  # inferred from the receiver, not from the aircraft

# Mode A codes that mean an emergency regardless of what the ADS-B emergency
# field says: hijack, radio failure, general emergency. Universal, not
# provider-specific.
EMERGENCY_SQUAWKS = frozenset({"7500", "7600", "7700"})


@dataclass(frozen=True, slots=True)
class AircraftState:
    """One aircraft's state vector at a point in time.

    Frozen: a new reading makes a new instance instead of mutating history.
    """

    icao24: str  # lower-case hex transponder address; the only guaranteed field
    callsign: Optional[str] = None
    origin_country: Optional[str] = None
    time_position: Optional[int] = None  # unix time of last position report
    last_contact: Optional[int] = None  # unix time of last message of any kind
    longitude: Optional[float] = None
    latitude: Optional[float] = None
    baro_altitude: Optional[float] = None  # metres
    on_ground: Optional[bool] = None
    velocity: Optional[float] = None  # m/s over ground
    true_track: Optional[float] = None  # degrees clockwise from north
    vertical_rate: Optional[float] = None  # m/s, positive = climb
    geo_altitude: Optional[float] = None  # metres, GNSS
    squawk: Optional[str] = None

    # --- identity, from the provider's aircraft database ---------------------
    # Not transmitted by the aircraft; looked up from its address. Absent for
    # anything not in the database, which is why these stay Optional.
    registration: Optional[str] = None  # tail number, e.g. "LZ-LAJ"
    aircraft_type: Optional[str] = None  # ICAO type code, e.g. "A320"
    description: Optional[str] = None  # human-readable, e.g. "AIRBUS A-320"
    category: Optional[str] = None  # ADS-B emitter class, A0-D7
    military: Optional[bool] = None

    # --- provenance ----------------------------------------------------------
    # How the position was obtained, which is not a detail: an aircraft with a
    # 50-minute-old position must not be drawn like one reporting live, and
    # without this the two are indistinguishable downstream.
    data_source: Optional[str] = None  # provider's `type`: adsb_icao, mode_s, mlat, ...
    position_source: Optional[str] = None  # one of POSITION_* below

    emergency: Optional[str] = None  # none, general, lifeguard, minfuel, nordo, ...

    @property
    def has_position(self) -> bool:
        """True when both coordinates are present, i.e. the state is mappable."""
        return self.latitude is not None and self.longitude is not None

    @property
    def is_emergency(self) -> bool:
        """True when the aircraft is declaring or squawking an emergency.

        Two independent channels: the ADS-B emergency/priority field, and the
        legacy Mode A squawks that predate it. An aircraft may set either, so
        checking one alone misses real emergencies.
        """
        return (
            self.emergency not in (None, "", "none")
            or self.squawk in EMERGENCY_SQUAWKS
        )

    @property
    def altitude(self) -> Optional[float]:
        """Best available altitude in metres, barometric preferred, GNSS fallback.

        State vectors routinely carry one but not the other.
        """
        return self.baro_altitude if self.baro_altitude is not None else self.geo_altitude


@dataclass(frozen=True, slots=True)
class RegionCell:
    """A snapped grid cell, used as the fan-out key.

    `key` names the cell's south-west corner, e.g. "40_-75". Kept a plain string
    so it works unchanged as a partition key, topic name or dict key.
    """

    key: str
    lamin: float
    lomin: float
    lamax: float
    lomax: float

    @property
    def bbox(self) -> Tuple[float, float, float, float]:
        """Cell as (lamin, lomin, lamax, lomax).

        The current provider queries point+radius (see geo.cell_to_point_radius),
        so nothing in the poll path reads this; it stays because the cell's
        extent is the definition of the cell, independent of how it's queried.
        """
        return (self.lamin, self.lomin, self.lamax, self.lomax)


@dataclass(frozen=True, slots=True)
class Connection:
    """One subscriber listening to one region cell.

    A client watching two cells is two Connections, which keeps
    list_connections_by_cell a lookup instead of a scan.
    """

    connection_id: str
    cell_key: str
    connected_at: float = 0.0  # unix time; used for reaping stale subscriptions
