"""$disconnect route -> drop the subscription -> arm a grace check if idle.

Thin adapter over the DynamoDB store, plus one scheduling call.

Polling is not disabled here. A page refresh is a disconnect followed within a
second or two by a connect, and cycling the EventBridge Rule on every refresh
would leave a returning client with no data until the next tick. Instead this
arms a one-shot check GRACE_DELAY_S out; grace_check_handler disables polling
only if the store is *still* empty by then.

Test locally:
    from lambdas.disconnect_handler import handler
    handler({"requestContext": {"connectionId": "abc"}}, None)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from botocore.exceptions import ClientError

from ._common import (
    GRACE_DELAY_S,
    GRACE_ROLE_ARN,
    GRACE_SCHEDULE_GROUP,
    GRACE_SCHEDULE_NAME,
    GRACE_TARGET_ARN,
    get_connection_store,
    get_scheduler,
)

logger = logging.getLogger(__name__)


def schedule_grace_check() -> None:
    """Create a one-shot schedule that re-checks emptiness after the grace period.

    ActionAfterCompletion=DELETE so the schedule removes itself once fired —
    otherwise every disconnect leaves a spent one-time schedule behind and the
    account slowly fills with them.

    A schedule of this name already existing means a previous disconnect armed
    one that has not fired yet. That is fine and not an error: one pending
    check is exactly what is wanted, and it is already timed later than any
    disconnect that preceded it.
    """
    if not GRACE_TARGET_ARN or not GRACE_ROLE_ARN:
        # Unset in local testing. Say so once rather than raising — the caller
        # is a disconnect, and there is nothing useful to fail.
        logger.warning(
            "grace schedule not armed: AEROFEED_GRACE_TARGET_ARN/ROLE_ARN unset"
        )
        return

    fire_at = datetime.now(timezone.utc) + timedelta(seconds=GRACE_DELAY_S)
    try:
        get_scheduler().create_schedule(
            Name=GRACE_SCHEDULE_NAME,
            GroupName=GRACE_SCHEDULE_GROUP,
            # No timezone suffix: at(...) is interpreted in ScheduleExpression
            # Timezone, which defaults to UTC — matching the UTC time above.
            ScheduleExpression=f"at({fire_at.strftime('%Y-%m-%dT%H:%M:%S')})",
            FlexibleTimeWindow={"Mode": "OFF"},
            ActionAfterCompletion="DELETE",
            Target={"Arn": GRACE_TARGET_ARN, "RoleArn": GRACE_ROLE_ARN},
            Description="aerofeed: disable polling if still no subscribers",
        )
        logger.info("armed grace check for %ss out", GRACE_DELAY_S)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConflictException":
            logger.info("grace check already armed; leaving it")
            return
        # Do not fail the disconnect over this. The cost of a missed schedule
        # is polling that stays on, which the next disconnect re-arms.
        logger.error("could not arm grace check: %s", exc)


def handler(event, context):
    """Remove a disconnecting client's subscriptions.

    Args:
        event: API Gateway $disconnect event. Needs
            requestContext.connectionId.
        context: Lambda context. Unused.

    Returns:
        {"statusCode": 200}. API Gateway ignores the body on $disconnect; the
        status matters only for logging.
    """
    connection_id = event.get("requestContext", {}).get("connectionId")
    if not connection_id:
        logger.error("disconnect event carried no connectionId")
        return {"statusCode": 400, "body": "missing connectionId"}

    # Every row, not one: a client at a wide zoom holds one per covered cell,
    # and $disconnect names no cell. Idempotent — disconnects arrive late,
    # duplicated, and for rows a 410 already reaped.
    store = get_connection_store()
    removed = store.delete_all_for_connection(connection_id)
    logger.info("disconnect %s (%d row(s) removed)", connection_id, removed)

    if store.count_connections() == 0:
        schedule_grace_check()

    return {"statusCode": 200}
