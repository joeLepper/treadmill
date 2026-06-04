"""Tests for treadmill_cli.identity.resolve_created_by."""

from __future__ import annotations

import pytest

from treadmill_cli.identity import SESSION_LABEL_ENV, resolve_created_by


def test_unset_env_explicit_none_falls_back_to_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USER", "joe")
    monkeypatch.delenv(SESSION_LABEL_ENV, raising=False)
    assert resolve_created_by(None) == "joe"


def test_unset_env_explicit_set_returns_explicit_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(SESSION_LABEL_ENV, raising=False)
    result = resolve_created_by("ad-hoc")
    assert result == "ad-hoc"
    assert capsys.readouterr().err == ""


def test_env_set_explicit_none_returns_env_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(SESSION_LABEL_ENV, "alan")
    result = resolve_created_by(None)
    assert result == "alan"
    assert capsys.readouterr().err == ""


def test_env_set_explicit_matches_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(SESSION_LABEL_ENV, "alan")
    result = resolve_created_by("alan")
    assert result == "alan"
    assert capsys.readouterr().err == ""


def test_env_set_explicit_mismatches_warns_and_uses_explicit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(SESSION_LABEL_ENV, "alan")
    result = resolve_created_by("carla")
    assert result == "carla"
    stderr = capsys.readouterr().err
    assert "warning" in stderr
    assert "alan" in stderr
    assert "carla" in stderr


def test_env_set_to_empty_string_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SESSION_LABEL_ENV, "")
    monkeypatch.setenv("USER", "joe")
    result = resolve_created_by(None)
    assert result == "joe"
