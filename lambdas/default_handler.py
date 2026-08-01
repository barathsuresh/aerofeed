"""$default route -> explain unmatched WebSocket messages.

API Gateway otherwise returns a bare Forbidden for messages that do not match a
route. Keeping a handler here gives clients and logs a concrete reason without
changing subscription state.
"""

from __future__ import annotations

import json
import logging

from botocore.exceptions import ClientError

from .subscribe_handler import endpoint_from
from ._common import get_management_api

logger = logging.getLogger(__name__)

GONE = ("GoneException", "410")


def handler(event, context):
    request_context = event.get("requestContext", {})
    connection_id = request_context.get("connectionId")
    if not connection_id:
        logger.error("default route event carried no connectionId")
        return {"statusCode": 400, "body": "missing connectionId"}

    endpoint = endpoint_from(event)
    if not endpoint:
        return {"statusCode": 200}

    frame = {
        "type": "error",
        "code": "unknown_message_type",
        "message": "Unknown message type. Expected subscribe.",
    }
    try:
        get_management_api(endpoint).post_to_connection(
            ConnectionId=connection_id,
            Data=json.dumps(frame).encode(),
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] in GONE:
            return {"statusCode": 200}
        logger.warning("could not send default-route error to %s: %s", connection_id, exc)

    return {"statusCode": 200}
