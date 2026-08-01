"""Shared wiring for the handlers. Not a handler itself.

A sixth file beyond the five specified, because the alternative is the same
boto3 clients and env lookups copy-pasted five times. Everything here is
configuration and construction — no behaviour.

Clients are built lazily and cached, not created at import. Both halves matter:

  cached — Lambda reuses a warm container across invocations, so one client per
    container rather than one per request saves a TLS handshake every time.
  lazy — boto3.client() raises NoRegionError when no region is configured, so
    building at import makes these modules unimportable anywhere that is not a
    configured AWS environment. Lambda always sets AWS_REGION, but a test
    runner and a developer laptop do not, and a handler you cannot import is a
    handler you cannot test.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache

import boto3

from aws.dynamo_store import DynamoConnectionStore, DynamoPositionStore
from core.geo import DEFAULT_MAX_CELLS
from aws.kinesis_stream import STREAM_NAME, KinesisStream

logger = logging.getLogger()
logger.setLevel(os.environ.get("AEROFEED_LOG_LEVEL", "INFO"))
# boto3 logs every request at INFO, which buries the handler's own output.
logging.getLogger("botocore").setLevel(logging.WARNING)

# --- configuration -----------------------------------------------------------

GRID_SIZE_DEGREES = float(os.environ.get("AEROFEED_GRID_SIZE", "5"))

# Most cells one client may cover. Defined in core.geo alongside the reasoning.
MAX_CELLS_PER_CLIENT = int(os.environ.get("AEROFEED_MAX_CELLS", str(DEFAULT_MAX_CELLS)))

# EventBridge Rule driving poller_handler. Disabled while nobody is connected,
# which is the whole cost argument: no subscribers, no invocations, no upstream
# calls, no Kinesis writes.
POLL_RULE_NAME = os.environ.get("AEROFEED_POLL_RULE", "aerofeed-poll-schedule")
POLLER_FUNCTION_NAME = os.environ.get("AEROFEED_POLLER_FUNCTION", "aerofeed-poller")

# EventBridge Scheduler one-shot that re-checks emptiness after a grace period.
GRACE_SCHEDULE_NAME = os.environ.get("AEROFEED_GRACE_SCHEDULE", "aerofeed-grace-check")
GRACE_SCHEDULE_GROUP = os.environ.get("AEROFEED_GRACE_GROUP", "default")
GRACE_DELAY_S = int(os.environ.get("AEROFEED_GRACE_DELAY", "60"))
GRACE_TARGET_ARN = os.environ.get("AEROFEED_GRACE_TARGET_ARN", "")
GRACE_ROLE_ARN = os.environ.get("AEROFEED_GRACE_ROLE_ARN", "")

# API Gateway Management API endpoint, e.g.
# https://abc123.execute-api.us-east-1.amazonaws.com/prod
# Set from the deployed stage; processor_handler has no request context to
# derive it from, unlike the connect and disconnect handlers.
WS_ENDPOINT = os.environ.get("AEROFEED_WS_ENDPOINT", "")

# Fallback subscription point when GeoIP cannot place the client.
DEFAULT_LAT = float(os.environ.get("AEROFEED_DEFAULT_LAT", "40.7"))
DEFAULT_LON = float(os.environ.get("AEROFEED_DEFAULT_LON", "-74.0"))


# --- lazily built, container-cached clients and stores -----------------------


@lru_cache(maxsize=1)
def get_connection_store() -> DynamoConnectionStore:
    return DynamoConnectionStore()


@lru_cache(maxsize=1)
def get_position_store() -> DynamoPositionStore:
    return DynamoPositionStore(grid_size_degrees=GRID_SIZE_DEGREES)


@lru_cache(maxsize=1)
def get_stream() -> KinesisStream:
    return KinesisStream(STREAM_NAME)


@lru_cache(maxsize=1)
def get_events():
    """EventBridge, for enabling and disabling the poll rule."""
    return boto3.client("events")


@lru_cache(maxsize=1)
def get_lambda():
    """Lambda client, used only to wake the poller on first connect."""
    return boto3.client("lambda")


@lru_cache(maxsize=1)
def get_scheduler():
    """EventBridge Scheduler, for the one-shot grace check."""
    return boto3.client("scheduler")


@lru_cache(maxsize=1)
def get_management_api(endpoint: str = ""):
    """API Gateway Management API client for pushing to WebSocket clients.

    Keyed on endpoint so a caller that derives it from its own request context
    does not collide with one reading it from the environment.
    """
    return boto3.client("apigatewaymanagementapi", endpoint_url=endpoint or WS_ENDPOINT)
