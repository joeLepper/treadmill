"""Unit tests for treadmill_api.repo_config (ADR-0050, decision 5).

Exercises the dict ↔ RepoConfig parser/serializer pair: defaults,
the auto-merge block flag, mode validation, the required ``repo`` field,
and round-trip equality.
"""

from __future__ import annotations

import pytest

from treadmill_api.repo_config import RepoConfig, parse_repo_config, to_dict


def test_parse_defaults_when_only_repo_given():
    cfg = parse_repo_config({"repo": "o/r"})

    assert cfg.repo == "o/r"
    assert cfg.mode == "conform"
    assert cfg.auto_merge_blocked is False
    assert cfg.test_command is None
    assert cfg.lint_command is None


def test_parse_auto_merge_blocked_flag():
    cfg = parse_repo_config({"repo": "o/r", "auto_merge_blocked": True})

    assert cfg.auto_merge_blocked is True


def test_parse_keeps_adapt_mode():
    cfg = parse_repo_config({"repo": "o/r", "mode": "adapt"})

    assert cfg.mode == "adapt"


def test_parse_rejects_bogus_mode():
    with pytest.raises(ValueError):
        parse_repo_config({"repo": "o/r", "mode": "bogus"})


def test_parse_requires_repo():
    with pytest.raises(ValueError):
        parse_repo_config({})


def test_round_trip_via_to_dict():
    source = {
        "repo": "o/r",
        "mode": "adapt",
        "auto_merge_blocked": True,
        "test_command": "pytest",
        "lint_command": "ruff check .",
    }

    assert to_dict(parse_repo_config(source)) == source


def test_round_trip_normalizes_defaults():
    """to_dict emits every field; parse(to_dict(parse(x))) is a fixed point."""
    once = parse_repo_config({"repo": "o/r"})
    twice = parse_repo_config(to_dict(once))

    assert once == twice
    assert isinstance(twice, RepoConfig)
