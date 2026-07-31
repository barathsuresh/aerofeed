"""Tests for the .env parser and the required-variable check.

airplanes.live needs no credentials, so `config` no longer requires anything at
import time and this module can import it directly — the env priming the
OpenSky client needed is gone with it.
"""

from __future__ import annotations

import os

import pytest

import config


def test_parses_pairs_comments_and_quotes(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n"
        "\n"
        "PLAIN=value\n"
        'QUOTED="quoted value"\n'
        "SPACED =  padded  \n"
        "malformed line with no equals\n"
    )
    monkeypatch.delenv("PLAIN", raising=False)
    monkeypatch.delenv("QUOTED", raising=False)
    monkeypatch.delenv("SPACED", raising=False)

    config.load_env_file(env_file)

    assert os.environ["PLAIN"] == "value"
    assert os.environ["QUOTED"] == "quoted value"
    assert os.environ["SPACED"] == "padded"


def test_real_environment_wins_over_file(tmp_path, monkeypatch):
    # The deployed case: a stray bundled .env must not shadow platform config.
    env_file = tmp_path / ".env"
    env_file.write_text("ALREADY_SET=from_file\n")
    monkeypatch.setenv("ALREADY_SET", "from_environment")

    config.load_env_file(env_file)

    assert os.environ["ALREADY_SET"] == "from_environment"


def test_missing_file_is_not_an_error(tmp_path):
    config.load_env_file(tmp_path / "does_not_exist")


@pytest.mark.parametrize("value", ["", "   "])
def test_require_rejects_empty_placeholder(monkeypatch, value):
    monkeypatch.setenv("SOME_KEY", value)
    with pytest.raises(RuntimeError, match="SOME_KEY"):
        config.require("SOME_KEY")


def test_require_rejects_unset(monkeypatch):
    monkeypatch.delenv("SOME_KEY", raising=False)
    with pytest.raises(RuntimeError, match="SOME_KEY"):
        config.require("SOME_KEY")


def test_require_returns_value(monkeypatch):
    monkeypatch.setenv("SOME_KEY", "  present  ")
    assert config.require("SOME_KEY") == "present"


def test_import_needs_no_credentials():
    """Importing config with an empty environment must not raise.

    The OpenSky migration's user-visible win: `python run_local.py` runs on a
    clean checkout with no .env at all.
    """
    assert not any(name.startswith("OPENSKY_") for name in vars(config))
