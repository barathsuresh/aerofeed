"""`subscribe` route -> move a client's cell coverage to its current viewport.

The piece that makes panning and zooming work once deployed. Without it a
client keeps whatever single cell $connect gave it from GeoIP, forever: the
poller only fetches cells someone is subscribed to, so panning shows empty sky
and the HUD never leaves its first cell.

All the logic is core/subscription.py, shared with local/local_ws_server.py.
The only difference is where the current cell set comes from — the local server
tracks it in the coroutine serving that socket, and a Lambda has no such
memory, so it reads it back from DynamoDB.

Test locally:
    from lambdas.subscribe_handler import handler
    handler({"requestContext": {"connectionId": "abc",
                                "domainName": "x.execute-api.us-east-1.amazonaws.com",
                                "stage": "prod"},
             "body": '{"type":"subscribe","bounds":[38,-76,47,-68]}'}, None)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict

from botocore.exceptions import ClientError

from core.subscription import (
    SubscriptionError,
    apply_subscription,
    is_subscribe,
    resolve_cells,
    subscribed_frame,
)

from ._common import (
    GRID_SIZE_DEGREES,
    MAX_CELLS_PER_CLIENT,
    get_connection_store,
    get_management_api,
    get_position_store,
)

logger = logging.getLogger(__name__)

GONE = ("GoneException", "410")


def endpoint_from(event) -> str:
    """Management API endpoint for the stage this request arrived on.

    Derived from the request rather than read from the environment: a route
    handler already knows which API and stage invoked it, and deriving means
    one less variable to keep in sync when the API is rebuilt.
    """
    context = event.get("requestContext", {})
    domain, stage = context.get("domainName"), context.get("stage")
    if not domain or not stage:
        return ""
    return f"https://{domain}/{stage}"


def handler(event, context):
    """Apply a client's new viewport.

    Args:
        event: API Gateway WebSocket route event. Needs
            requestContext.connectionId and a JSON `body` carrying
            {"type": "subscribe", "bounds": [lamin, lomin, lamax, lomax]}
            or {"type": "subscribe", "lat": ..., "lon": ...}.
        context: Lambda context. Unused.

    Returns:
        {"statusCode": 200} once applied. A bad message is still 200 — the
        client keeps the subscription it already had, and failing the route
        would only make API Gateway retry a message that cannot get better.
    """
    request_context = event.get("requestContext", {})
    connection_id = request_context.get("connectionId")
    if not connection_id:
        logger.error("subscribe event carried no connectionId")
        return {"statusCode": 400, "body": "missing connectionId"}

    try:
        message = json.loads(event.get("body") or "{}")
    except ValueError:
        logger.warning("unparseable body from %s", connection_id)
        return {"statusCode": 200}

    if not is_subscribe(message):
        # Routed here but not a subscribe. Nothing to do, and not an error.
        return {"statusCode": 200}

    try:
        wanted = resolve_cells(message, GRID_SIZE_DEGREES, MAX_CELLS_PER_CLIENT)
    except SubscriptionError as exc:
        # A client sending nonsense keeps the aircraft it already has.
        logger.warning("ignoring bad subscribe from %s: %s", connection_id, exc)
        return {"statusCode": 200}

    store = get_connection_store()
    current = set(store.list_cells_for_connection(connection_id))
    wanted_keys = {cell.key for cell in wanted}

    if wanted_keys == current:
        # Moving within the same coverage is the common case — the client
        # cannot know where the 5-degree boundaries are, so it re-sends on
        # every map move and the server is what makes that cheap.
        return {"statusCode": 200}

    updated = apply_subscription(store, connection_id, current, wanted, time.time())
    logger.info(
        "resubscribe %s: %d -> %d cells (%s)",
        connection_id, len(current), len(updated), ",".join(sorted(updated)),
    )

    _confirm(event, connection_id, wanted)

    # Only the newly-added cells: the client already has aircraft for the ones
    # it was keeping, and re-sending them would be pure duplication.
    _send_snapshot(event, connection_id, wanted_keys - current)
    return {"statusCode": 200}


def _send_snapshot(event, connection_id: str, new_cells: set) -> None:
    """Push the current state of newly-covered cells straight away.

    The data is already in DynamoDB, written by the processor moments ago.
    Without this the client waits for the next poll and then receives only the
    aircraft that moved, because the delta filter suppresses everything else
    until the heartbeat reaches it — up to two minutes to fill a map that could
    be filled in under a second.

    Best-effort throughout: the subscription is already written, and a client
    that misses the snapshot still populates the slow way.
    """
    if not new_cells:
        return

    endpoint = endpoint_from(event)
    if not endpoint:
        return

    api = get_management_api(endpoint)
    positions = get_position_store()
    sent = 0
    attempted = 0
    for cell in sorted(new_cells):
        for state in positions.list_positions_in_cell(cell):
            if not state.has_position:
                continue
            attempted += 1
            frame = {
                "type": "aircraft",
                "region_cell": cell,
                "state": asdict(state),
                # Lets the client distinguish a replayed snapshot from a live
                # update, e.g. to avoid animating a hundred markers into place.
                "snapshot": True,
            }
            try:
                api.post_to_connection(
                    ConnectionId=connection_id, Data=json.dumps(frame).encode()
                )
                sent += 1
            except ClientError as exc:
                if exc.response["Error"]["Code"] in GONE:
                    get_connection_store().delete_all_for_connection(connection_id)
                    return
                logger.warning(
                    "snapshot incomplete for %s after %d/%d sends: %s",
                    connection_id, sent, attempted, exc,
                )
                _send_snapshot_status(event, connection_id, sent, complete=False)
                return
    logger.info("snapshot: sent %d aircraft to %s", sent, connection_id)
    if attempted:
        _send_snapshot_status(event, connection_id, sent, complete=True)


def _send_snapshot_status(event, connection_id: str, sent: int, complete: bool) -> None:
    """Tell the client whether the best-effort snapshot completed."""
    endpoint = endpoint_from(event)
    if not endpoint:
        return

    frame = {"type": "snapshot_status", "sent": sent, "complete": complete}
    try:
        get_management_api(endpoint).post_to_connection(
            ConnectionId=connection_id, Data=json.dumps(frame).encode()
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] in GONE:
            get_connection_store().delete_all_for_connection(connection_id)
            return
        logger.warning("could not send snapshot status to %s: %s", connection_id, exc)


def _confirm(event, connection_id: str, wanted: list) -> None:
    """Tell the client which cells it now has.

    Best-effort: the subscription is already written, and a client that missed
    the confirmation still receives aircraft for the new cells. Failing the
    route here would undo nothing and retry nothing useful.
    """
    endpoint = endpoint_from(event)
    if not endpoint:
        logger.warning("no endpoint in request context; skipping confirmation")
        return

    frame = subscribed_frame(connection_id, wanted, MAX_CELLS_PER_CLIENT)
    try:
        get_management_api(endpoint).post_to_connection(
            ConnectionId=connection_id, Data=json.dumps(frame).encode()
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] in GONE:
            # Disconnected between writing the subscription and confirming it.
            removed = get_connection_store().delete_all_for_connection(connection_id)
            logger.info("connection %s gone; reaped %d row(s)", connection_id, removed)
            return
        logger.warning("could not confirm subscription to %s: %s", connection_id, exc)
