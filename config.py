"""Setting resolution. Deliberately outside `core/`.

`core` takes its settings as arguments and knows nothing about where they come
from; this module is the one place that reads the environment, so the domain
logic stays testable with literal values.

Resolution order: real environment first, `.env` second. Real environment wins
so a deployed platform's configured vars are never shadowed by a stray `.env`
baked into a bundle.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from core.geo import DEFAULT_MAX_CELLS

ENV_FILE = Path(__file__).resolve().parent / ".env"


def load_env_file(path: Path = ENV_FILE) -> None:
    """Load `KEY=value` lines from `path` into os.environ, if it exists.

    A missing file is normal, not an error — that is the deployed case, where
    the platform supplies the variables.
    """
    # override=False keeps the real environment authoritative.
    load_dotenv(path, override=False)


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

# airplanes.live needs no credentials, so nothing is required at import any
# more. `require` stays for the next setting that is genuinely mandatory —
# deleting and restoring it is more churn than leaving eight lines in place.

# --- Local pipeline settings -------------------------------------------------
# Defaults are chosen so `python run_local.py` works with no configuration at
# all. Every value is env-overridable for experimentation.

DB_PATH = Path(os.environ.get("AEROFEED_DB_PATH", "local.db"))

WS_HOST = os.environ.get("AEROFEED_WS_HOST", "127.0.0.1")
WS_PORT = int(os.environ.get("AEROFEED_WS_PORT", "8765"))

GRID_SIZE_DEGREES = float(os.environ.get("AEROFEED_GRID_SIZE", "5"))

# Local cadence, tightened for iteration speed. The binding constraint is now
# airplanes.live's 1 req/s and one request per active cell, which the poller
# paces; see local_scheduler for the full note.
POLL_INTERVAL_S = float(os.environ.get("AEROFEED_POLL_INTERVAL", "15"))

# Fallback subscription point for clients that pass no lat/lon override.
# Loopback addresses carry no geolocation, so local clients need a default;
# New York has dense, near-continuous traffic, which makes for a live demo.
DEFAULT_LAT = float(os.environ.get("AEROFEED_DEFAULT_LAT", "40.7"))
DEFAULT_LON = float(os.environ.get("AEROFEED_DEFAULT_LON", "-74.0"))

# Most region cells one client may cover at once. Defined in core.geo, which
# owns what the cap means; overridable per deployment.
MAX_CELLS_PER_CLIENT = int(os.environ.get("AEROFEED_MAX_CELLS", str(DEFAULT_MAX_CELLS)))
