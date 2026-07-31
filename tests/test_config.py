"""Tests for the .env parser and the required-variable check.

`config` resolves credentials at import, so these are set before importing it.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("OPENSKY_CLIENT_ID", "test-id")
os.environ.setdefault("OPENSKY_CLIENT_SECRET", "test-secret")

import config  # noqa: E402  — must follow the env setup above


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
