"""Convenience shim so `python run_local.py` works from the repo root.

The real entrypoint is local/run_local.py; this exists only so the documented
command works from where you naturally stand.
"""

from local.run_local import cli

if __name__ == "__main__":
    cli()
