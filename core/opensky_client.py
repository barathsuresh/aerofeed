"""OpenSky REST client: OAuth2 token management + state polling.

OpenSky retired Basic auth for OAuth2 client credentials via Keycloak. Tokens
last ~30 minutes, so one is cached and refreshed just before expiry —
re-authenticating per request would burn the rate-limited token endpoint and add
a round trip to every poll.

Credentials are passed in, not discovered. No cloud SDKs, no logging framework.
"""

from __future__ import annotations

import time
from typing import Any, Optional, Sequence

import requests

from .models import AircraftState

TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network"
    "/protocol/openid-connect/token"
)
STATES_URL = "https://opensky-network.org/api/states/all"

# Covers clock skew plus the flight time of the request about to use the token:
# a token valid at send time but expired in transit fails with a 401 that a
# retry cannot tell apart from bad credentials. Raise if 401s appear under load.
TOKEN_REFRESH_MARGIN_S = 30.0

# (connect, read). Large bounding boxes are genuinely slow; connect stays tight
# so a black-holed host fails fast.
DEFAULT_TIMEOUT = (5.0, 30.0)

# Used only if the token response omits expires_in. The docs state 30 minutes;
# assuming it beats assuming zero, which would re-authenticate on every poll.
# A token revoked early still gets caught by the 401 retry.
DEFAULT_TOKEN_LIFETIME_S = 1800.0

# The API returns bare arrays, so these indices are the only record of the wire
# format. Keep in sync with https://openskynetwork.github.io/opensky-api/rest.html
_IDX_ICAO24 = 0
_IDX_CALLSIGN = 1
_IDX_ORIGIN_COUNTRY = 2
_IDX_TIME_POSITION = 3
_IDX_LAST_CONTACT = 4
_IDX_LONGITUDE = 5
_IDX_LATITUDE = 6
_IDX_BARO_ALTITUDE = 7
_IDX_ON_GROUND = 8
_IDX_VELOCITY = 9
_IDX_TRUE_TRACK = 10
_IDX_VERTICAL_RATE = 11
_IDX_GEO_ALTITUDE = 13
_IDX_SQUAWK = 14


class OpenSkyError(RuntimeError):
    """OpenSky unreachable or answering unusably.

    One type, because callers have one sensible response to any of them: skip
    this poll, keep the previous states, retry next tick.
    """


def _at(row: Sequence[Any], index: int) -> Any:
    """row[index], or None when the row is short.

    OpenSky has extended the state vector over time and truncated rows do occur.
    """
    return row[index] if index < len(row) else None


def _opt_float(value: Any) -> Optional[float]:
    """Coerce to float, else None. Bools excluded — float(True) would give 1.0."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value: Any) -> Optional[int]:
    """Coerce to int, else None."""
    number = _opt_float(value)
    return None if number is None else int(number)


def _opt_str(value: Any) -> Optional[str]:
    """Coerce to a stripped string, else None.

    Callsigns are space-padded ("DLH441  ") and an absent one is all spaces,
    which must become None so downstream truthiness checks behave.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_state(row: Sequence[Any]) -> Optional[AircraftState]:
    """Parse one positional state vector into an AircraftState.

    Every field except icao24 is optional: nulls, missing trailing elements and
    unexpected types yield None for that field, never an exception. A row is
    rejected only when icao24 is unusable — without an identity the state can't
    be stored, diffed or addressed.

    Returns:
        The parsed state, or None if the row should be skipped.
    """
    if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
        return None

    icao24 = _opt_str(_at(row, _IDX_ICAO24))
    if icao24 is None:
        return None

    on_ground = _at(row, _IDX_ON_GROUND)

    return AircraftState(
        # Normalise case so one aircraft never occupies two store keys.
        icao24=icao24.lower(),
        callsign=_opt_str(_at(row, _IDX_CALLSIGN)),
        origin_country=_opt_str(_at(row, _IDX_ORIGIN_COUNTRY)),
        time_position=_opt_int(_at(row, _IDX_TIME_POSITION)),
        last_contact=_opt_int(_at(row, _IDX_LAST_CONTACT)),
        longitude=_opt_float(_at(row, _IDX_LONGITUDE)),
        latitude=_opt_float(_at(row, _IDX_LATITUDE)),
        baro_altitude=_opt_float(_at(row, _IDX_BARO_ALTITUDE)),
        # Real bools only: coercing would invent takeoff/landing events in delta.
        on_ground=on_ground if isinstance(on_ground, bool) else None,
        velocity=_opt_float(_at(row, _IDX_VELOCITY)),
        true_track=_opt_float(_at(row, _IDX_TRUE_TRACK)),
        vertical_rate=_opt_float(_at(row, _IDX_VERTICAL_RATE)),
        geo_altitude=_opt_float(_at(row, _IDX_GEO_ALTITUDE)),
        squawk=_opt_str(_at(row, _IDX_SQUAWK)),
    )


class OpenSkyClient:
    """OpenSky client with a cached access token.

    ponytail: not thread-safe — two threads would each fetch a token. The
    pipeline polls from one worker, so a lock is dead weight until it doesn't.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        session: Optional[requests.Session] = None,
        timeout: tuple[float, float] = DEFAULT_TIMEOUT,
    ) -> None:
        """Create a client.

        Args:
            client_id: OAuth2 client id from the OpenSky account page.
            client_secret: Matching secret.
            session: Optional session, injected in tests and to share pooling.
            timeout: (connect, read) applied to every request.
        """
        self._client_id = client_id
        self._client_secret = client_secret
        self._session = session or requests.Session()
        self._timeout = timeout

        self._token: Optional[str] = None
        # Monotonic, not wall clock: expiry is a duration from now, and an NTP
        # jump would make a live token look expired or vice versa.
        self._token_expires_at: float = 0.0

    def _token_is_fresh(self) -> bool:
        """True when a cached token exists and is not near expiry."""
        return (
            self._token is not None
            and time.monotonic() < self._token_expires_at - TOKEN_REFRESH_MARGIN_S
        )

    def get_token(self, force_refresh: bool = False) -> str:
        """Return a valid access token, fetching only when needed.

        Args:
            force_refresh: Discard the cache and re-authenticate. Used after a
                401, where the server rejected a token we believed was valid.

        Raises:
            OpenSkyError: Authentication failed or the response was malformed.
        """
        if not force_refresh and self._token_is_fresh():
            return self._token  # type: ignore[return-value]

        try:
            response = self._session.post(
                TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise OpenSkyError(f"token request failed: {exc}") from exc
        except ValueError as exc:
            raise OpenSkyError("token response was not valid JSON") from exc

        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not token:
            raise OpenSkyError("token response contained no access_token")

        # Read the clock before deriving the deadline; a later reading would
        # push our expiry past the server's and shrink the safety margin.
        issued_at = time.monotonic()
        expires_in = _opt_float(payload.get("expires_in")) or DEFAULT_TOKEN_LIFETIME_S

        self._token = str(token)
        self._token_expires_at = issued_at + expires_in
        return self._token

    def get_states(
        self,
        lamin: float,
        lomin: float,
        lamax: float,
        lomax: float,
    ) -> list[AircraftState]:
        """Fetch all aircraft states inside a bounding box.

        Args:
            lamin: Southern edge, decimal degrees.
            lomin: Western edge.
            lamax: Northern edge.
            lomax: Eastern edge.

        Returns:
            Parsed states. Unidentifiable rows are dropped; an empty box gives
            an empty list (OpenSky returns "states": null when nothing is up).

        Raises:
            OpenSkyError: Transport failure, non-2xx, or unparseable body.
        """
        params = {"lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax}

        payload = self._get_states_payload(params, force_refresh=False)

        states = payload.get("states") if isinstance(payload, dict) else None
        if not states:
            return []

        parsed = (parse_state(row) for row in states)
        return [state for state in parsed if state is not None]

    def _get_states_payload(self, params: dict[str, float], force_refresh: bool) -> Any:
        """Issue the states request, re-authenticating once on a 401."""
        token = self.get_token(force_refresh=force_refresh)
        try:
            response = self._session.get(
                STATES_URL,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self._timeout,
            )
            # Token rejected early (revoked server-side). Retry exactly once —
            # a second 401 means the credentials are bad, and looping would just
            # hammer the auth endpoint.
            if response.status_code == 401 and not force_refresh:
                return self._get_states_payload(params, force_refresh=True)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            raise OpenSkyError(f"states request failed: {exc}") from exc
        except ValueError as exc:
            raise OpenSkyError("states response was not valid JSON") from exc
