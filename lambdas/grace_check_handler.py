"""One-shot EventBridge Scheduler target -> stop polling if still nobody home.

The second half of the disconnect path. disconnect_handler arms this rather
than disabling polling itself, because a page refresh is a disconnect followed
immediately by a connect and cycling the rule on every refresh would leave the
returning client with no data until the next tick.

By the time this runs, either someone reconnected — in which case do nothing
and leave polling on — or the store is still empty and polling should stop.
That check is the whole function.

Idempotent and safe to fire late. The schedule that invokes it deletes itself
via ActionAfterCompletion=DELETE, so there is nothing to clean up here.

Test locally:
    from lambdas.grace_check_handler import handler
    handler({}, None)
"""

from __future__ import annotations

import logging

from botocore.exceptions import ClientError

from ._common import POLL_RULE_NAME, get_connection_store, get_events

logger = logging.getLogger(__name__)


def disable_polling() -> None:
    """Turn the poller's EventBridge Rule off.

    DisableRule is idempotent, so this is not guarded by a read of the rule's
    current state — a redundant call is cheaper than the extra API round trip
    it would take to avoid one.
    """
    try:
        get_events().disable_rule(Name=POLL_RULE_NAME)
        logger.info("disabled poll rule %s", POLL_RULE_NAME)
    except ClientError as exc:
        # Worth an error: this failing means polling — and the upstream calls
        # and Kinesis writes behind it — continues with nobody listening.
        logger.error("could not disable %s: %s", POLL_RULE_NAME, exc)


def handler(event, context):
    """Disable polling if the connection store is still empty.

    Args:
        event: Scheduler payload. Unused — the decision comes entirely from
            the store's current contents, not from anything the schedule
            carried when it was armed a minute ago.
        context: Lambda context. Unused.

    Returns:
        {"connections": int, "polling_disabled": bool}
    """
    remaining = get_connection_store().count_connections()

    if remaining > 0:
        # Someone reconnected inside the grace window. Leave polling alone —
        # this is the case the grace period exists for.
        logger.info("%d connection(s) present; leaving polling enabled", remaining)
        return {"connections": remaining, "polling_disabled": False}

    logger.info("still no subscribers; disabling polling")
    disable_polling()
    return {"connections": 0, "polling_disabled": True}
