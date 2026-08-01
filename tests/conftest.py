"""Suite-wide guard: no test may reach AWS.

A handler test that forgets to patch a store silently falls through to a real
boto3 client and scans the live table — which passed CI, cost money, and only
surfaced when a new assertion happened to count the rows. Failing loudly beats
finding out from a bill.

Credentials are blanked rather than the network being blocked, so the failure
reads as an auth error naming the call that escaped.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_aws(monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "test-credentials-not-real")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    # A profile in the shared config file would override the fake keys above.
    monkeypatch.delenv("AWS_PROFILE", raising=False)
