"""Unit tests for the context-doc router (ADR-0054).

Builds a minimal FastAPI app with only the context-doc router and overrides
``get_session`` with an in-memory stub session + ``get_context_store`` with a
fake store. ``OnboardingStore`` is monkeypatched on the router module so its
async methods record what would have been written. No live DB, S3, or
network traffic.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api.dependencies_db import get_session
from treadmill_api.routers import context_docs as context_docs_router_mod
from treadmill_api.routers.context_docs import get_context_store


class _StubSession:
    """Minimal async-session stub for the PUT/GET handlers.

    The list handler uses ``session.execute`` directly; tests that hit
    LIST set ``execute_result`` to a value whose ``.all()`` returns the
    expected fake rows. ``commit`` is recorded for PUT assertions.
    """

    def __init__(self) -> None:
        self.committed = False
        self.execute_result: Any = None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        return None

    async def execute(self, _stmt: Any) -> Any:
        return self.execute_result


class _FakeStore:
    """Records the put_doc + presigned_get_url calls the handler issues."""

    def __init__(self) -> None:
        self.put_calls: list[tuple[str, str]] = []
        self.presign_calls: list[str] = []
        self.next_key = "repo-context/owner/name/deadbeef.md"
        self.next_url = "https://signed.example/get"

    def put_doc(self, repo: str, content: str) -> str:
        self.put_calls.append((repo, content))
        return self.next_key

    def presigned_get_url(self, key: str) -> str:
        self.presign_calls.append(key)
        return self.next_url


def _build_app(
    session: _StubSession,
    store: _FakeStore | None,
    monkeypatch: pytest.MonkeyPatch,
    *,
    bucket: str | None = "ctx-bucket",
) -> FastAPI:
    app = FastAPI()
    app.include_router(context_docs_router_mod.router)
    app.state.settings = SimpleNamespace(
        context_docs_bucket=bucket, aws_region="us-east-1",
    )

    def _session_override() -> Iterator[_StubSession]:
        yield session

    app.dependency_overrides[get_session] = _session_override
    if store is not None:
        app.dependency_overrides[get_context_store] = lambda: store
    return app


# ── PUT: writes content, records version, returns 200 ────────────────────────


def test_put_context_doc_records_and_returns_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _StubSession()
    store = _FakeStore()
    store.next_key = "repo-context/owner/name/abc123.md"
    app = _build_app(session, store, monkeypatch)

    record = AsyncMock(return_value=3)
    monkeypatch.setattr(
        context_docs_router_mod,
        "OnboardingStore",
        lambda: SimpleNamespace(record_context_doc=record),
    )

    with TestClient(app) as client:
        response = client.put(
            "/api/v1/repos/owner/name/docs/adrs/0001-x.md",
            json={"content": "hello world"},
        )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "repo": "owner/name",
        "doc_path": "adrs/0001-x.md",
        "version": 3,
    }
    assert store.put_calls == [("owner/name", "hello world")]
    assert session.committed
    record.assert_awaited_once()
    args = record.await_args.args
    # (session, repo, doc_path, s3_key, content_sha)
    assert args[1] == "owner/name"
    assert args[2] == "adrs/0001-x.md"
    assert args[3] == "repo-context/owner/name/abc123.md"
    # sha256("hello world")
    assert args[4] == (
        "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    )


# ── GET: returns presigned url + version ─────────────────────────────────────


def test_get_context_doc_returns_url_and_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _StubSession()
    store = _FakeStore()
    store.next_url = "https://signed.example/get?sig=abc"
    app = _build_app(session, store, monkeypatch)

    row = SimpleNamespace(version=5, s3_key="repo-context/owner/name/xyz.md")
    get_doc = AsyncMock(return_value=row)
    monkeypatch.setattr(
        context_docs_router_mod,
        "OnboardingStore",
        lambda: SimpleNamespace(get_context_doc=get_doc),
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/repos/owner/name/docs/adrs/0001-x.md",
        )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "repo": "owner/name",
        "doc_path": "adrs/0001-x.md",
        "version": 5,
        "url": "https://signed.example/get?sig=abc",
    }
    assert store.presign_calls == ["repo-context/owner/name/xyz.md"]
    get_doc.assert_awaited_once()


def test_get_context_doc_missing_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _StubSession()
    store = _FakeStore()
    app = _build_app(session, store, monkeypatch)

    monkeypatch.setattr(
        context_docs_router_mod,
        "OnboardingStore",
        lambda: SimpleNamespace(get_context_doc=AsyncMock(return_value=None)),
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/repos/owner/name/docs/missing.md",
        )

    assert response.status_code == 404, response.text
    assert store.presign_calls == []


# ── LIST: returns latest version per doc_path ────────────────────────────────


def test_list_context_docs_returns_summaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _StubSession()
    store = _FakeStore()
    # session.execute(...).all() → list of rows with doc_path + version.
    rows = [
        SimpleNamespace(doc_path="AGENT.md", version=2),
        SimpleNamespace(doc_path="adrs/0001-x.md", version=4),
    ]
    result = MagicMock()
    result.all.return_value = rows
    session.execute_result = result

    app = _build_app(session, store, monkeypatch)

    with TestClient(app) as client:
        response = client.get("/api/v1/repos/owner/name/docs")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "repo": "owner/name",
        "docs": [
            {"doc_path": "AGENT.md", "version": 2},
            {"doc_path": "adrs/0001-x.md", "version": 4},
        ],
    }


# ── 503 when bucket unconfigured ─────────────────────────────────────────────


def test_endpoints_503_when_bucket_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``CONTEXT_DOCS_BUCKET`` is unset, ``get_context_store`` raises
    503 — same shape as the GitHub-App-not-configured path."""
    session = _StubSession()
    # Do NOT override get_context_store so the real dependency runs and
    # observes the unset bucket on app.state.settings.
    app = _build_app(session, None, monkeypatch, bucket=None)

    with TestClient(app) as client:
        put_resp = client.put(
            "/api/v1/repos/owner/name/docs/adrs/0001-x.md",
            json={"content": "hi"},
        )
        get_resp = client.get(
            "/api/v1/repos/owner/name/docs/adrs/0001-x.md",
        )
        list_resp = client.get("/api/v1/repos/owner/name/docs")

    assert put_resp.status_code == 503
    assert get_resp.status_code == 503
    assert list_resp.status_code == 503
