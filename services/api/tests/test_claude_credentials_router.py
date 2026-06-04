"""Unit tests for the Claude credential resolver (ADR-0055).

Builds a minimal FastAPI app with the router mounted and overrides
``get_session`` + the ``boto3``-backed secrets factory + ``OnboardingStore``
so the tests assert routing and failure modes without a live DB or AWS.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api.dependencies_db import get_session
from treadmill_api.repo_config import RepoConfig
from treadmill_api.routers import claude_credentials as router_mod


class _StubSession:
    async def commit(self) -> None:  # pragma: no cover — never hit here
        pass

    async def rollback(self) -> None:
        return None


class _FakeStore:
    """Returns whatever RepoConfig the test seeded for the asked repo."""

    def __init__(self) -> None:
        self.by_repo: dict[str, RepoConfig] = {}

    async def get_repo_config(
        self, session: Any, repo: str
    ) -> RepoConfig | None:
        return self.by_repo.get(repo)


class _FakeSecretsManager:
    """Captures GetSecretValue calls; returns the configured SecretString."""

    def __init__(self, by_id: dict[str, str | None] | None = None,
                 raise_for: str | None = None) -> None:
        self.by_id = by_id or {}
        self.raise_for = raise_for
        self.calls: list[str] = []

    def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:
        self.calls.append(SecretId)
        if self.raise_for == SecretId:
            raise RuntimeError("simulated SM error")
        value = self.by_id.get(SecretId)
        if value is None:
            return {}
        return {"SecretString": value}


def _build_app(
    *,
    accounts_json: str | None,
    default_account: str | None,
    store: _FakeStore,
    sm: _FakeSecretsManager,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router_mod.router)
    # Stand-in for ``Settings``: anything with the two attributes works.
    settings = type(
        "S", (), {
            "claude_accounts_json": accounts_json,
            "claude_default_account": default_account,
            "aws_region": "us-west-2",
        }
    )()
    app.state.settings = settings

    def _session_override() -> Iterator[_StubSession]:
        yield _StubSession()

    app.dependency_overrides[get_session] = _session_override
    monkeypatch.setattr(router_mod, "OnboardingStore", lambda: store)
    monkeypatch.setattr(
        router_mod, "_make_secrets_client", lambda region: sm
    )
    return app


# ── 503: no accounts configured ──────────────────────────────────────────────


def test_503_when_accounts_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _build_app(
        accounts_json=None, default_account=None,
        store=_FakeStore(), sm=_FakeSecretsManager(), monkeypatch=monkeypatch,
    )
    with TestClient(app) as client:
        r = client.post("/api/v1/claude/credentials", json={"repo": "o/r"})
    assert r.status_code == 503
    assert "no claude accounts configured" in r.text.lower()


def test_503_when_accounts_json_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_app(
        accounts_json="{not json", default_account="x",
        store=_FakeStore(), sm=_FakeSecretsManager(), monkeypatch=monkeypatch,
    )
    with TestClient(app) as client:
        r = client.post("/api/v1/claude/credentials", json={"repo": "o/r"})
    assert r.status_code == 503
    assert "not valid json" in r.text.lower()


# ── 503: no per-repo account and no default ─────────────────────────────────


def test_503_when_no_per_repo_and_no_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = {
        "primary": {"type": "oauth", "secret_name": "treadmill/claude-primary"}
    }
    store = _FakeStore()
    # No row for this repo → no claude_account at repo level.
    app = _build_app(
        accounts_json=json.dumps(accounts), default_account=None,
        store=store, sm=_FakeSecretsManager(), monkeypatch=monkeypatch,
    )
    with TestClient(app) as client:
        r = client.post("/api/v1/claude/credentials", json={"repo": "o/r"})
    assert r.status_code == 503
    assert "no claude_account" in r.text.lower()
    assert "claude_default_account" in r.text.lower()


# ── happy path: oauth via deployment default ────────────────────────────────


def test_returns_oauth_token_via_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = {
        "primary": {"type": "oauth", "secret_name": "treadmill/claude-primary"},
    }
    sm = _FakeSecretsManager(
        by_id={"treadmill/claude-primary": "sk-oauth-token-value"}
    )
    app = _build_app(
        accounts_json=json.dumps(accounts), default_account="primary",
        store=_FakeStore(), sm=sm, monkeypatch=monkeypatch,
    )
    with TestClient(app) as client:
        r = client.post("/api/v1/claude/credentials", json={"repo": "o/r"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "repo": "o/r",
        "account": "primary",
        "type": "oauth",
        "token": "sk-oauth-token-value",
        "fallback": None,
    }
    assert sm.calls == ["treadmill/claude-primary"]


# ── whitespace/newline in the stored secret is stripped ─────────────────────


def test_strips_whitespace_and_newline_from_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A trailing newline in the stored secret made the Bearer header an invalid
    # HTTP header value — claude exited 1 with "Header has invalid value",
    # crashlooping the worker (2026-06-04 medicoder incident). The resolver must
    # strip surrounding whitespace so a sloppily-stored secret can't break auth.
    accounts = {
        "primary": {"type": "oauth", "secret_name": "treadmill/claude-primary"},
    }
    sm = _FakeSecretsManager(
        by_id={"treadmill/claude-primary": "  sk-oauth-token-value\n"}
    )
    app = _build_app(
        accounts_json=json.dumps(accounts), default_account="primary",
        store=_FakeStore(), sm=sm, monkeypatch=monkeypatch,
    )
    with TestClient(app) as client:
        r = client.post("/api/v1/claude/credentials", json={"repo": "o/r"})
    assert r.status_code == 200, r.text
    assert r.json()["token"] == "sk-oauth-token-value"   # stripped, no \n


def test_502_when_secret_is_only_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A secret that is only whitespace strips to empty → treated as no token.
    accounts = {
        "primary": {"type": "oauth", "secret_name": "treadmill/claude-primary"},
    }
    sm = _FakeSecretsManager(by_id={"treadmill/claude-primary": "   \n"})
    app = _build_app(
        accounts_json=json.dumps(accounts), default_account="primary",
        store=_FakeStore(), sm=sm, monkeypatch=monkeypatch,
    )
    with TestClient(app) as client:
        r = client.post("/api/v1/claude/credentials", json={"repo": "o/r"})
    assert r.status_code == 502, r.text


# ── happy path: api_key via explicit per-repo override ──────────────────────


def test_returns_api_key_via_repo_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = {
        "primary":  {"type": "oauth",  "secret_name": "treadmill/claude-primary"},
        "secondary": {"type": "api_key", "secret_name": "treadmill/claude-secondary"},
    }
    sm = _FakeSecretsManager(
        by_id={"treadmill/claude-secondary": "sk-ant-api-key-value"}
    )
    store = _FakeStore()
    store.by_repo["acme/widget"] = RepoConfig(
        repo="acme/widget", claude_account="secondary",
    )
    app = _build_app(
        accounts_json=json.dumps(accounts), default_account="primary",
        store=store, sm=sm, monkeypatch=monkeypatch,
    )
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/claude/credentials", json={"repo": "acme/widget"}
        )
    assert r.status_code == 200, r.text
    body = r.json()
    # Repo-level override beats the default; type comes through unchanged.
    assert body["account"] == "secondary"
    assert body["type"] == "api_key"
    assert body["token"] == "sk-ant-api-key-value"
    assert sm.calls == ["treadmill/claude-secondary"]


# ── 404: account name not in the configured map (no cross-account fallback) ──


def test_404_when_account_name_not_in_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = {
        "primary": {"type": "oauth", "secret_name": "treadmill/claude-primary"},
    }
    store = _FakeStore()
    store.by_repo["acme/widget"] = RepoConfig(
        repo="acme/widget", claude_account="missing",
    )
    app = _build_app(
        accounts_json=json.dumps(accounts), default_account="primary",
        store=store, sm=_FakeSecretsManager(), monkeypatch=monkeypatch,
    )
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/claude/credentials", json={"repo": "acme/widget"}
        )
    # Critical: no silent fallback to ``primary`` — explicit 404.
    assert r.status_code == 404
    assert "missing" in r.text


# ── 502: secrets manager fails / empty ──────────────────────────────────────


def test_502_when_secrets_manager_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = {
        "primary": {"type": "oauth", "secret_name": "treadmill/claude-primary"},
    }
    sm = _FakeSecretsManager(raise_for="treadmill/claude-primary")
    app = _build_app(
        accounts_json=json.dumps(accounts), default_account="primary",
        store=_FakeStore(), sm=sm, monkeypatch=monkeypatch,
    )
    with TestClient(app) as client:
        r = client.post("/api/v1/claude/credentials", json={"repo": "o/r"})
    assert r.status_code == 502


def test_502_when_secret_has_no_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = {
        "primary": {"type": "oauth", "secret_name": "treadmill/claude-primary"},
    }
    sm = _FakeSecretsManager(by_id={"treadmill/claude-primary": None})
    app = _build_app(
        accounts_json=json.dumps(accounts), default_account="primary",
        store=_FakeStore(), sm=sm, monkeypatch=monkeypatch,
    )
    with TestClient(app) as client:
        r = client.post("/api/v1/claude/credentials", json={"repo": "o/r"})
    assert r.status_code == 502


# ── 503: account entry malformed (wrong type / missing field) ───────────────


def test_503_when_account_entry_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = {
        "primary": {"type": "bogus", "secret_name": "x"},
    }
    app = _build_app(
        accounts_json=json.dumps(accounts), default_account="primary",
        store=_FakeStore(), sm=_FakeSecretsManager(), monkeypatch=monkeypatch,
    )
    with TestClient(app) as client:
        r = client.post("/api/v1/claude/credentials", json={"repo": "o/r"})
    assert r.status_code == 503
    assert "primary" in r.text


# ── ADR-0066 fallback credential ─────────────────────────────────────────────


def test_fallback_none_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = {
        "primary": {"type": "oauth", "secret_name": "treadmill/claude-primary"},
    }
    sm = _FakeSecretsManager(
        by_id={"treadmill/claude-primary": "sk-oauth-token-value"}
    )
    store = _FakeStore()
    # RepoConfig with no claude_account_fallback set.
    store.by_repo["o/r"] = RepoConfig(repo="o/r", claude_account="primary")
    app = _build_app(
        accounts_json=json.dumps(accounts), default_account="primary",
        store=store, sm=sm, monkeypatch=monkeypatch,
    )
    with TestClient(app) as client:
        r = client.post("/api/v1/claude/credentials", json={"repo": "o/r"})
    assert r.status_code == 200, r.text
    assert r.json()["fallback"] is None


def test_fallback_populated_with_valid_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = {
        "primary":  {"type": "oauth",   "secret_name": "treadmill/claude-primary"},
        "fallback": {"type": "api_key", "secret_name": "treadmill/claude-fallback"},
    }
    sm = _FakeSecretsManager(by_id={
        "treadmill/claude-primary":  "sk-oauth-primary",
        "treadmill/claude-fallback": "sk-api-fallback",
    })
    store = _FakeStore()
    store.by_repo["o/r"] = RepoConfig(
        repo="o/r", claude_account="primary", claude_account_fallback="fallback"
    )
    app = _build_app(
        accounts_json=json.dumps(accounts), default_account="primary",
        store=store, sm=sm, monkeypatch=monkeypatch,
    )
    with TestClient(app) as client:
        r = client.post("/api/v1/claude/credentials", json={"repo": "o/r"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["account"] == "primary"
    assert body["token"] == "sk-oauth-primary"
    assert body["fallback"] == {
        "account": "fallback",
        "type": "api_key",
        "token": "sk-api-fallback",
    }


def test_fallback_none_when_account_not_in_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = {
        "primary": {"type": "oauth", "secret_name": "treadmill/claude-primary"},
    }
    sm = _FakeSecretsManager(
        by_id={"treadmill/claude-primary": "sk-oauth-primary"}
    )
    store = _FakeStore()
    store.by_repo["o/r"] = RepoConfig(
        repo="o/r", claude_account="primary", claude_account_fallback="missing"
    )
    app = _build_app(
        accounts_json=json.dumps(accounts), default_account="primary",
        store=store, sm=sm, monkeypatch=monkeypatch,
    )
    with TestClient(app) as client:
        r = client.post("/api/v1/claude/credentials", json={"repo": "o/r"})
    # Primary succeeds; fallback absent in map → best-effort None, not 404.
    assert r.status_code == 200, r.text
    assert r.json()["fallback"] is None


def test_fallback_none_when_secret_fetch_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = {
        "primary":  {"type": "oauth", "secret_name": "treadmill/claude-primary"},
        "fallback": {"type": "oauth", "secret_name": "treadmill/claude-fallback"},
    }
    sm = _FakeSecretsManager(
        by_id={"treadmill/claude-primary": "sk-oauth-primary"},
        raise_for="treadmill/claude-fallback",
    )
    store = _FakeStore()
    store.by_repo["o/r"] = RepoConfig(
        repo="o/r", claude_account="primary", claude_account_fallback="fallback"
    )
    app = _build_app(
        accounts_json=json.dumps(accounts), default_account="primary",
        store=store, sm=sm, monkeypatch=monkeypatch,
    )
    with TestClient(app) as client:
        r = client.post("/api/v1/claude/credentials", json={"repo": "o/r"})
    # Primary succeeds; fallback SM error → best-effort None, not 502.
    assert r.status_code == 200, r.text
    assert r.json()["fallback"] is None


def test_fallback_none_when_secret_has_no_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = {
        "primary":  {"type": "oauth", "secret_name": "treadmill/claude-primary"},
        "fallback": {"type": "oauth", "secret_name": "treadmill/claude-fallback"},
    }
    sm = _FakeSecretsManager(by_id={
        "treadmill/claude-primary":  "sk-oauth-primary",
        "treadmill/claude-fallback": None,
    })
    store = _FakeStore()
    store.by_repo["o/r"] = RepoConfig(
        repo="o/r", claude_account="primary", claude_account_fallback="fallback"
    )
    app = _build_app(
        accounts_json=json.dumps(accounts), default_account="primary",
        store=store, sm=sm, monkeypatch=monkeypatch,
    )
    with TestClient(app) as client:
        r = client.post("/api/v1/claude/credentials", json={"repo": "o/r"})
    # Primary succeeds; fallback secret has no SecretString → best-effort None.
    assert r.status_code == 200, r.text
    assert r.json()["fallback"] is None
