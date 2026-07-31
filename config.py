"""Credential and setting resolution. Deliberately outside `core/`.

`core` takes its credentials as arguments and knows nothing about where they
come from; this module is the one place that reads the environment, so the
domain logic stays testable with literal strings.

Resolution order: real environment first, `.env` second. Real environment wins
so a deployed Lambda's configured vars are never shadowed by a stray `.env`
baked into the bundle.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent / ".env"


def load_env_file(path: Path = ENV_FILE) -> None:
    """Load `KEY=value` lines from `path` into os.environ, if it exists.

    A missing file is normal, not an error — that is the deployed case, where
    the platform supplies the variables. Existing variables are never
    overwritten.

    Enough of the dotenv format for our two keys: comments, blank lines and
    optional surrounding quotes. ponytail: no multi-line values, no `export`
    prefix, no interpolation. Reach for python-dotenv if that day comes.
    """
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # setdefault, not assignment: the real environment must win.
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def require(name: str) -> str:
    """Return a non-empty environment variable, or fail with a usable message.

    Empty counts as missing — an unfilled `.env` placeholder should fail here,
    not surface later as a confusing 401 from the auth server.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill it in, "
            f"or set {name} in the deployment environment."
        )
    return value


load_env_file()

# Resolved at import so a misconfigured deploy dies at cold start with a clear
# message, rather than on the first poll. Never log these.
OPENSKY_CLIENT_ID = require("OPENSKY_CLIENT_ID")
OPENSKY_CLIENT_SECRET = require("OPENSKY_CLIENT_SECRET")
