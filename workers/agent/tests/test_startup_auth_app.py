"""Tests for the GitHub App worker-auth path (ADR-0049 phase 5b)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

import pytest

from treadmill_agent import startup_auth
from treadmill_agent.startup_auth import StartupAuthError


def _settings(api_url: str = "http://treadmill-api:8088") -> SimpleNamespace:
    return SimpleNamespace(api_url=api_url)


class _FakeResp:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_via_app_mints_then_pipes_to_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[list[str], bytes | None]] = []
    monkeypatch.setattr(
        startup_auth.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeResp(json.dumps({"token": "ghs_inst"}).encode()),
    )

    def fake_run(args, **kw):  # type: ignore[no-untyped-def]
        captured.append((args, kw.get("input")))
        return mock.MagicMock(returncode=0, stderr=b"")

    monkeypatch.setattr(startup_auth.subprocess, "run", fake_run)

    startup_auth.bootstrap_github_auth_via_app(settings=_settings())

    # First call: gh auth login --with-token, token via stdin (never argv).
    assert captured[0][0] == ["gh", "auth", "login", "--with-token"]
    assert captured[0][1] == b"ghs_inst"
    # Second: install the git credential helper.
    assert captured[1][0] == ["gh", "auth", "setup-git"]


def test_via_app_posts_to_installation_token_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        return _FakeResp(json.dumps({"token": "ghs_x"}).encode())

    monkeypatch.setattr(startup_auth.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        startup_auth.subprocess, "run",
        lambda args, **kw: mock.MagicMock(returncode=0, stderr=b""),
    )

    startup_auth.bootstrap_github_auth_via_app(settings=_settings("http://api:9/"))

    assert seen["url"] == "http://api:9/api/v1/github/installation-token"
    assert seen["method"] == "POST"


def test_via_app_raises_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(req, timeout=None):  # type: ignore[no-untyped-def]
        raise OSError("connection refused")

    monkeypatch.setattr(startup_auth.urllib.request, "urlopen", boom)
    with pytest.raises(StartupAuthError):
        startup_auth.bootstrap_github_auth_via_app(settings=_settings())


def test_via_app_raises_on_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        startup_auth.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeResp(json.dumps({}).encode()),
    )
    with pytest.raises(StartupAuthError):
        startup_auth.bootstrap_github_auth_via_app(settings=_settings())


def test_via_app_raises_when_gh_login_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        startup_auth.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeResp(json.dumps({"token": "x"}).encode()),
    )
    monkeypatch.setattr(
        startup_auth.subprocess, "run",
        lambda args, **kw: mock.MagicMock(returncode=1, stderr=b"bad token"),
    )
    with pytest.raises(StartupAuthError):
        startup_auth.bootstrap_github_auth_via_app(settings=_settings())
