"""Core domain models. Data only — no persistence, transport or cloud concerns.

OpenSky nulls every field except icao24, so almost everything here is Optional.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


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

    @property
    def has_position(self) -> bool:
        """True when both coordinates are present, i.e. the state is mappable."""
        return self.latitude is not None and self.longitude is not None

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
        """Cell as (lamin, lomin, lamax, lomax) — OpenSky's parameter order."""
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
