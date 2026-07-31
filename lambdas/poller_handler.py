"""EventBridge Rule -> poll every subscribed cell -> Kinesis.

Thin adapter. All the behaviour is Poller.poll_once() from local/poller.py: it
reads the active cells, converts each to a point+radius query, paces calls for
the provider's 1 req/s limit and publishes to the stream. This file exists to
turn an EventBridge tick into that call and a response dict.

No credentials are loaded. airplanes.live needs none, so this function's IAM
role wants exactly:

    kinesis:PutRecord              on the stream
    dynamodb:Scan, dynamodb:Query  on ws-connections and its GSI
    logs:*                         the usual

Nothing Secrets-Manager-related, here or anywhere else in this architecture.

Test locally:
    from lambdas.poller_handler import handler
    handler({"source": "aws.events"}, None)
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache

from core.airplanes_live_client import AirplanesLiveClient
from local.poller import Poller

from ._common import GRID_SIZE_DEGREES, get_connection_store, get_stream

logger = logging.getLogger(__name__)

# Module scope: the client holds a requests.Session, so a warm container reuses
# its connection pool instead of renegotiating TLS on every invocation. Safe to
# build eagerly — unlike a boto3 client it needs no region or credentials.
client = AirplanesLiveClient()


@lru_cache(maxsize=1)
def get_poller() -> Poller:
    """Built once per container; deferred so importing needs no AWS config."""
    return Poller(
        client,
        get_connection_store(),
        get_stream(),
        grid_size_degrees=GRID_SIZE_DEGREES,
    )


def handler(event, context):
    """Poll once and report what was published.

    Args:
        event: EventBridge scheduled event. Unused — the rule carries no
            payload; which cells to poll comes from the connection store.
        context: Lambda context. Unused.

    Returns:
        {"published": int, "cells": int}

    Note:
        poll_once() sleeps 1.1s between cells to respect the provider's rate
        limit, and in Lambda that sleep is billed wall time. Nine cells is
        ~9s of duration per invocation, almost all of it waiting. That is the
        cost of the rate limit, not of Lambda; if it matters, the fix is
        fanning cells out across concurrent invocations, each polling one cell,
        which trades the sleep for coordination.
    """
    cells = list(get_connection_store().list_active_cells())
    if not cells:
        # Should not happen — the rule is disabled when nobody is connected —
        # but a disable that lost a race is not an error, just a wasted tick.
        logger.info("no active cells; nothing to poll")
        return {"published": 0, "cells": 0}

    # asyncio.run because poll_once is async: it overlaps the blocking HTTP
    # call with the event loop in the long-lived local process. Here there is
    # nothing to overlap with, so this just drives it to completion.
    published = asyncio.run(get_poller().poll_once())

    logger.info("polled %d cell(s), published %d record(s)", len(cells), published)
    return {"published": published, "cells": len(cells)}
