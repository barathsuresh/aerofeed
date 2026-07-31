"""airplanes.live REST client: point+radius state polling.

No authentication — the provider is open, so there is no token to cache, refresh
or leak. Everything the OpenSky client spent on OAuth2 is simply gone.

Two differences from OpenSky shape the rest of this module:

  * Queries are point+radius, not bounding box. Callers hold a RegionCell, so
    `core.geo.cell_to_point_radius` does the conversion.
  * The wire format is ADS-B native: feet, knots, ages-before-`now` instead of
    timestamps, and the literal string "ground" where OpenSky sent a separate
    on_ground boolean. All of it is normalised here so AircraftState stays
    metric, absolute and provider-agnostic — delta thresholds and the frontend
    keep their existing meaning.

Several fields have two possible sources that do not overlap (`baro_rate` vs
`geom_rate`, `track` vs `true_heading`). Each has a fallback rather than a
single lookup, because picking one source leaves a large minority of real
aircraft reporting nothing for a value the response plainly contains.

Rate limit is 1 request/second. This client makes exactly one call per
get_states(), so it does not sleep; a caller looping over several cells owns
that pacing (see local/poller.py).
"""

from __future__ import annotations

from typing import Any, Optional

import requests

from .models import (
    POSITION_ESTIMATED,
    POSITION_LAST_KNOWN,
    POSITION_LIVE,
    AircraftState,
)

BASE_URL = "https://api.airplanes.live/v2"

# The provider rejects anything larger. Callers clamp before asking, but the
# constant lives here because it is a property of the API, not of the grid.
MAX_RADIUS_NM = 250

# (connect, read). Read stays generous for a full-radius query; connect stays
# tight so a black-holed host fails fast.
DEFAULT_TIMEOUT = (5.0, 30.0)

# The wire format is ADS-B native. AircraftState is documented in metres and
# m/s, and delta.ALTITUDE_EPSILON_M is metres, so converting here is what keeps
# those numbers meaning what they say.
FEET_TO_METRES = 0.3048
KNOTS_TO_M_S = 0.514444
FEET_PER_MIN_TO_M_S = FEET_TO_METRES / 60.0

# alt_baro carries this instead of a number for anything on the surface. It is
# constant in real data, not an edge case, and it is the only signal this
# provider gives for on-ground state.
GROUND = "ground"

# The envelope's `now` is documented as "seconds since Jan 1 1970" but is
# actually milliseconds on the wire (observed: 1785516434001). Treating it as
# seconds would date every position to the year 58500, so the unit is asserted
# from the magnitude rather than trusted from the docs.
MILLIS_PER_SECOND = 1000.0
_SECONDS_UPPER_BOUND = 1e11  # ~year 5138 in seconds, ~1973 in milliseconds


class AirplanesLiveError(RuntimeError):
    """airplanes.live unreachable or answering unusably.

    One type, because callers have one sensible response to any of them: skip
    this poll, keep the previous states, retry next tick.
    """


def _opt_float(value: Any) -> Optional[float]:
    """Coerce to float, else None. Bools excluded — float(True) would give 1.0."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_str(value: Any) -> Optional[str]:
    """Coerce to a stripped string, else None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_callsign(value: Any) -> Optional[str]:
    """Normalise the `flight` field to a bare callsign.

    Real values are space-padded and, for aircraft transmitting a registration
    rather than a flight number, prefixed with dots: ".N954JB " -> "N954JB".
    An all-padding value must become None so downstream truthiness checks (the
    frontend falls back to icao24 for the label) behave.
    """
    text = _opt_str(value)
    if text is None:
        return None
    # Strip after the whitespace strip: ". N954JB" would otherwise keep a space.
    return text.lstrip(".").strip() or None


def parse_now(payload: Any) -> Optional[float]:
    """Read the envelope's generation time as unix seconds.

    Returned rather than falling back to the local clock: `seen`/`seen_pos` are
    ages measured against *this* value, so mixing in a local reading would fold
    our own clock skew and the request's flight time into every timestamp.
    No `now` means no timestamps, which is honest.
    """
    if not isinstance(payload, dict):
        return None
    now = _opt_float(payload.get("now"))
    if now is None or now <= 0:
        return None
    # Accept either unit so a provider fix doesn't silently shift every
    # timestamp by a factor of 1000.
    return now if now < _SECONDS_UPPER_BOUND else now / MILLIS_PER_SECOND


def _age_to_timestamp(age_s: Any, now_s: Optional[float]) -> Optional[int]:
    """Convert an age-in-seconds-before-`now` into a unix timestamp.

    The provider reports staleness as an age (`seen`, `seen_pos`); the model
    stores absolute times. Without a `now` to anchor against there is nothing
    to convert, so the field stays None.
    """
    age = _opt_float(age_s)
    if age is None or now_s is None:
        return None
    return int(now_s - age)


def _parse_track(record: dict) -> Optional[float]:
    """Best available direction of travel, degrees clockwise from north.

    `track` (ground track) is what the frontend rotates the icon by, and it is
    present on every airborne record. Surface aircraft mostly omit it and send
    a heading instead — in a live 272-aircraft sample, 43 of 47 ground records
    had no `track`, of which 38 carried `true_heading` or `mag_heading`.
    Without the fallback every taxiing aircraft renders pointing due north.

    Heading is where the nose points, not where the aircraft is going; on the
    surface the difference is negligible, which is why it is only a fallback.
    """
    for field in ("track", "true_heading", "mag_heading"):
        value = _opt_float(record.get(field))
        if value is not None:
            return value
    return None


def _parse_altitude(value: Any) -> tuple[Optional[float], Optional[bool]]:
    """Split alt_baro into (metres, on_ground).

    Three normal cases, none of them exceptional:
        "ground" -> no altitude, definitely on the surface
        a number -> altitude in feet, definitely airborne
        absent   -> both unknown; absent is not the same as False, and
                    claiming False would invent a takeoff in delta.py
    """
    if value == GROUND:
        return None, True
    feet = _opt_float(value)
    if feet is None:
        return None, None
    return feet * FEET_TO_METRES, False


def _parse_position(
    record: dict, now_s: Optional[float]
) -> tuple[Optional[float], Optional[float], Optional[str], Optional[int]]:
    """Resolve the best available position, and say where it came from.

    Three sources in descending quality, because an aircraft with no live fix
    is not the same as no aircraft:

      lat/lon        a current fix. What the vast majority of records carry.
      lastPosition   a real fix the provider considers expired (>60s old). The
                     aircraft is still transmitting — Mode S transponders send
                     altitude and speed but no position at all — so dropping it
                     loses a live target over a stale coordinate.
      rr_lat/rr_lon  no fix at all; a rough guess from the receiver's own
                     location. Good to within tens of miles, useless for
                     navigation, still better than a blank map.

    Returning the source alongside is the point: a caller that cannot tell
    these apart will draw a 50-minute-old guess as though it were live.

    Returns:
        (latitude, longitude, position_source, time_position). All None when
        no source has usable coordinates.
    """
    latitude = _opt_float(record.get("lat"))
    longitude = _opt_float(record.get("lon"))
    if latitude is not None and longitude is not None:
        return (
            latitude,
            longitude,
            POSITION_LIVE,
            _age_to_timestamp(record.get("seen_pos"), now_s),
        )

    last = record.get("lastPosition")
    if isinstance(last, dict):
        latitude = _opt_float(last.get("lat"))
        longitude = _opt_float(last.get("lon"))
        if latitude is not None and longitude is not None:
            # Its own seen_pos, not the record's — this timestamp is the whole
            # reason the position is flagged stale rather than shown as current.
            return (
                latitude,
                longitude,
                POSITION_LAST_KNOWN,
                _age_to_timestamp(last.get("seen_pos"), now_s),
            )

    latitude = _opt_float(record.get("rr_lat"))
    longitude = _opt_float(record.get("rr_lon"))
    if latitude is not None and longitude is not None:
        # No timestamp: an estimate derived from the receiver was never
        # "reported" at any particular moment, and inventing a time for it
        # would make a guess look like a measurement.
        return latitude, longitude, POSITION_ESTIMATED, None

    return None, None, None, None


def _parse_military(record: dict) -> Optional[bool]:
    """Read the military bit out of dbFlags.

    dbFlags is a bitfield (military=1, interesting=2, PIA=4, LADD=8). Absent
    for most aircraft, which means unknown rather than civilian — the flag is
    only set for entries the provider's database has classified.
    """
    flags = record.get("dbFlags")
    if flags is None or isinstance(flags, bool):
        return None
    try:
        return bool(int(flags) & 1)
    except (TypeError, ValueError):
        return None


def _parse_vertical_rate(record: dict) -> Optional[float]:
    """Best available rate of climb in m/s, barometric preferred, GNSS fallback.

    Neither source is universal and they do not overlap: in a 272-aircraft
    capture, 75 records had only `baro_rate`, 50 had only `geom_rate` and 44
    had neither. Without the fallback that middle group reports no vertical
    movement at all despite the data being right there.

    Barometric first to match AircraftState.altitude's preference, so a single
    aircraft's altitude and its rate of change come from the same source
    wherever possible.
    """
    for field in ("baro_rate", "geom_rate"):
        feet_per_min = _opt_float(record.get(field))
        if feet_per_min is not None:
            return feet_per_min * FEET_PER_MIN_TO_M_S
    return None


def parse_state(record: Any, now_s: Optional[float] = None) -> Optional[AircraftState]:
    """Parse one `ac` entry into an AircraftState.

    Every field except hex is optional and genuinely missing in real responses
    — `flight` is absent entirely on many records, not merely blank. Missing,
    null and unexpected types all yield None for that field, never an
    exception. A record is rejected only when hex is unusable: without an
    identity the state can't be stored, diffed or addressed.

    Args:
        record: One entry from the response's `ac` array.
        now_s: The envelope's generation time in unix seconds, from
            parse_now(). Omitted, the two timestamp fields stay None — every
            other field parses the same either way.

    Returns:
        The parsed state, or None if the record should be skipped.
    """
    if not isinstance(record, dict):
        return None

    icao24 = _opt_str(record.get("hex"))
    if icao24 is None:
        return None

    baro_altitude, on_ground = _parse_altitude(record.get("alt_baro"))
    geo_feet = _opt_float(record.get("alt_geom"))
    ground_speed_kt = _opt_float(record.get("gs"))
    latitude, longitude, position_source, time_position = _parse_position(record, now_s)

    return AircraftState(
        # Normalise case so one aircraft never occupies two store keys.
        icao24=icao24.lower(),
        callsign=_parse_callsign(record.get("flight")),
        # Ages before the envelope's `now`, converted to absolute times. These
        # are what distinguish "parked and quiet" from "transponder dropped
        # forty seconds ago" — the aircraft looks identical either way.
        time_position=time_position,
        last_contact=_age_to_timestamp(record.get("seen"), now_s),
        latitude=latitude,
        longitude=longitude,
        baro_altitude=baro_altitude,
        on_ground=on_ground,
        velocity=None if ground_speed_kt is None else ground_speed_kt * KNOTS_TO_M_S,
        true_track=_parse_track(record),
        vertical_rate=_parse_vertical_rate(record),
        geo_altitude=None if geo_feet is None else geo_feet * FEET_TO_METRES,
        squawk=_opt_str(record.get("squawk")),
        registration=_opt_str(record.get("r")),
        aircraft_type=_opt_str(record.get("t")),
        description=_opt_str(record.get("desc")),
        category=_opt_str(record.get("category")),
        military=_parse_military(record),
        data_source=_opt_str(record.get("type")),
        position_source=position_source,
        emergency=_opt_str(record.get("emergency")),
        # origin_country genuinely has no equivalent: this provider carries
        # registration and operator (`r`, `ownOp`) but no country of registry.
    )


class AirplanesLiveClient:
    """airplanes.live client. Stateless apart from the pooled HTTP session."""

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
    ) -> None:
        """Create a client.

        Args:
            session: Optional session, injected in tests and to share pooling.
            timeout: (connect, read) applied to every request.
        """
        self._session = session or requests.Session()
        self._timeout = timeout

    def get_states(self, lat: float, lon: float, radius_nm: float) -> list[AircraftState]:
        """Fetch all aircraft within `radius_nm` of a point.

        One request, no sleeping: a caller polling several cells per cycle owns
        the 1 req/s pacing, because only it knows how many calls are coming.

        Args:
            lat: Centre latitude, decimal degrees.
            lon: Centre longitude, decimal degrees.
            radius_nm: Search radius in nautical miles. Clamped to
                MAX_RADIUS_NM — the API 400s above it, and a silently dropped
                poll is worse than a slightly small circle.

        Returns:
            Parsed states. Unidentifiable records are dropped; empty sky gives
            an empty list.

        Raises:
            AirplanesLiveError: Transport failure, non-2xx, or unparseable body.
        """
        # Both operands floated so the URL renders the same way whether or not
        # the clamp bit — min() otherwise returns the bare int and yields "250".
        radius = min(float(radius_nm), float(MAX_RADIUS_NM))
        url = f"{BASE_URL}/point/{lat}/{lon}/{radius}"

        try:
            response = self._session.get(url, timeout=self._timeout)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise AirplanesLiveError(f"states request failed: {exc}") from exc
        except ValueError as exc:
            raise AirplanesLiveError("states response was not valid JSON") from exc

        aircraft = payload.get("ac") if isinstance(payload, dict) else None
        if not aircraft:
            return []

        # One clock reading for the whole batch: every age in this response is
        # measured against this same `now`, so re-reading per record would make
        # aircraft in one snapshot disagree about when the snapshot happened.
        now_s = parse_now(payload)

        parsed = (parse_state(record, now_s) for record in aircraft)
        return [state for state in parsed if state is not None]
