"""$connect route -> place the client -> register -> wake the poller if idle.

Thin adapter. snap_to_grid is core/geo.py's, the write is the DynamoDB store's;
this file turns an API Gateway connect event into those calls.

Placement chain: `?lat=&lon=` on the connect URL, then GeoIP on the source IP,
then the configured default. The query override comes first so a client can say
where it wants to watch regardless of where it is connecting from.

Test locally:
    from lambdas.connect_handler import handler
    handler({"requestContext": {"connectionId": "abc",
                                "identity": {"sourceIp": "49.207.1.1"}}}, None)
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request

from botocore.exceptions import ClientError

from core.geo import snap_to_grid
from core.models import Connection

from ._common import (
    DEFAULT_LAT,
    DEFAULT_LON,
    GRID_SIZE_DEGREES,
    POLLER_FUNCTION_NAME,
    POLL_RULE_NAME,
    get_connection_store,
    get_events,
    get_lambda,
)

logger = logging.getLogger(__name__)

# Free tier, no key, 45 req/min. HTTP only — the free endpoint does not serve
# HTTPS, so this call leaves the VPC in clear text carrying an IP address.
#
# Swap for a MaxMind GeoLite2 Lambda layer to drop the external call entirely:
# the .mmdb ships in the layer, lookups become local and sub-millisecond, and
# there is no third-party dependency in the connect path — where an outage or a
# rate limit currently costs every new client a 2s timeout. Left as an HTTP
# call for now because a layer needs a MaxMind licence key and a build step.
GEOIP_URL = "http://ip-api.com/json/{ip}?fields=status,lat,lon,city"
GEOIP_TIMEOUT_S = 2.0


def geoip(source_ip: str):
    """Approximate a location from an IP, or None.

    Never raises: a failed lookup must not fail the connection. The caller
    falls back to the configured default, which is a worse guess, not a broken
    one.

    Cells are 5 degrees (~550km), so city-level accuracy is already far finer
    than the grid — precision beyond this rounds away entirely.
    """
    if not source_ip:
        return None
    try:
        request = urllib.request.Request(
            GEOIP_URL.format(ip=source_ip),
            headers={"User-Agent": "aerofeed"},
        )
        with urllib.request.urlopen(request, timeout=GEOIP_TIMEOUT_S) as response:
            payload = json.loads(response.read())
    except Exception as exc:
        # Timeout, rate limit, DNS, malformed body. All mean the same here.
        logger.warning("geoip lookup failed for %s: %s", source_ip, exc)
        return None

    if payload.get("status") != "success":
        # Private and reserved ranges answer "fail" — normal when testing.
        logger.info("geoip could not place %s: %s", source_ip, payload.get("status"))
        return None

    lat, lon = payload.get("lat"), payload.get("lon")
    if lat is None or lon is None:
        return None
    return float(lat), float(lon), payload.get("city") or "unknown"


def resolve_location(event):
    """Where this client should be subscribed, and how that was decided."""
    params = event.get("queryStringParameters") or {}
    if params.get("lat") is not None and params.get("lon") is not None:
        try:
            return float(params["lat"]), float(params["lon"]), "query override"
        except (TypeError, ValueError):
            # A typo'd URL should still get a working subscription.
            logger.warning("ignoring non-numeric lat/lon: %r", params)

    source_ip = (
        event.get("requestContext", {}).get("identity", {}).get("sourceIp", "")
    )
    located = geoip(source_ip)
    if located is not None:
        lat, lon, city = located
        return lat, lon, f"geoip {city}"

    return DEFAULT_LAT, DEFAULT_LON, "default"


def enable_polling() -> None:
    """Turn the poller's EventBridge Rule on.

    Called unconditionally once this connection is the only one. Not guarded by
    a read of the rule's current state: EnableRule is idempotent, and a second
    call is far cheaper than a race in which two simultaneous first-connections
    both decide the other will do it and neither does.
    """
    try:
        get_events().enable_rule(Name=POLL_RULE_NAME)
        logger.info("enabled poll rule %s", POLL_RULE_NAME)
    except ClientError as exc:
        # A missing rule must not fail the connection — the client is
        # registered and will receive data as soon as polling resumes.
        logger.error("could not enable %s: %s", POLL_RULE_NAME, exc)


def invoke_initial_poll() -> None:
    """Kick one poll immediately so first load does not wait for the next tick."""
    try:
        get_lambda().invoke(
            FunctionName=POLLER_FUNCTION_NAME,
            InvocationType="Event",
        )
        logger.info("invoked initial poll via %s", POLLER_FUNCTION_NAME)
    except ClientError as exc:
        # The scheduled rule is already enabled, so this only costs latency.
        logger.error("could not invoke initial poll %s: %s", POLLER_FUNCTION_NAME, exc)


def handler(event, context):
    """Register a subscription for a connecting client.

    Args:
        event: API Gateway $connect event. Needs
            requestContext.connectionId and, for GeoIP,
            requestContext.identity.sourceIp.
        context: Lambda context. Unused.

    Returns:
        {"statusCode": 200} to accept the connection. Any non-2xx would make
        API Gateway reject the handshake.
    """
    request_context = event.get("requestContext", {})
    connection_id = request_context.get("connectionId")
    if not connection_id:
        logger.error("connect event carried no connectionId")
        return {"statusCode": 400, "body": "missing connectionId"}

    lat, lon, source = resolve_location(event)
    try:
        cell = snap_to_grid(lat, lon, GRID_SIZE_DEGREES)
    except ValueError as exc:
        # Coordinates out of range, from a hand-edited query string.
        logger.warning("rejecting %s: %s", connection_id, exc)
        return {"statusCode": 400, "body": str(exc)}

    # Counted before the write, so "was it empty" is not confused by our own
    # row. Advisory under concurrency, and deliberately so: the cost of being
    # wrong is one redundant EnableRule call.
    store = get_connection_store()
    was_idle = store.count_connections() == 0

    store.put_connection(
        Connection(
            connection_id=connection_id,
            cell_key=cell.key,
            connected_at=time.time(),
        )
    )
    logger.info(
        "connect %s -> cell %s (%s, %.3f/%.3f)", connection_id, cell.key, source, lat, lon
    )

    # One cell on connect. A client that zooms out widens this by sending
    # {"type":"subscribe", bounds:[...]} on a separate route — $connect carries
    # no body, and the viewport is not known until the map has laid out.
    if was_idle:
        enable_polling()
        invoke_initial_poll()

    return {"statusCode": 200}
