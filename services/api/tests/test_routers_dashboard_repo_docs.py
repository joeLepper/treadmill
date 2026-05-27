"""Unit tests for ``GET /api/v1/dashboard/repos/{repo}/docs`` (ADR-0056 PR-B3).

The repo-docs surface reads the ADR-0054 ``repo_context_docs`` index
through ``OnboardingStore.list_repo_docs`` and reshapes it into the
``RepoDocs`` payload the dashboard's ``useRepoDocs`` hook consumes
(``services/dashboard/src/api/types.ts``):

    { arch: string, plans: int, last_updated: datetime }

These tests pin the three explicit failure modes called out in
``routers/dashboard/repo_docs.py``'s module docstring:

  * Happy path — a repo with ``arch.md`` + 3 ``plans/*`` docs returns
    the typed shape (``arch`` = the arch doc_path, ``plans`` = 3,
    ``last_updated`` = the most-recent row's ``created_at``).
  * 404 — a repo with **no** indexed docs (the LIST returns ``[]``)
    raises ``404`` rather than returning a zeroed payload that the
    dashboard would render as "0 plans, never updated".
  * 503 — when ``CONTEXT_DOCS_BUCKET`` is unset, the shared
    ``get_context_store`` dependency raises ``503`` before the handler
    runs, mirroring ``routers/context_docs.py``.

A stub session + a monkeypatched ``OnboardingStore`` keep the test
hermetic — no real Postgres, S3, or boto3 calls.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api.dependencies_db import get_session
from treadmill_api.routers.dashboard import repo_docs as repo_docs_mod


class _StubSession:
    """Minimal async-session stub — the handler only passes it through
    to ``OnboardingStore.list_repo_docs`` (which we monkeypatch), so the
    stub doesn't need to dispatch any SQL itself."""

    async def execute(self, _stmt: Any) -> Any:  # pragma: no cover
        raise AssertionError(
            "stub session should not receive direct execute() calls — the "
            "router goes through OnboardingStore.list_repo_docs (patched in "
            "tests)",
        )


def _build_app(
    session: _StubSession,
    *,
    bucket: str | None = "ctx-bucket",
) -> FastAPI:
    app = FastAPI()
    app.include_router(repo_docs_mod.router, prefix="/api/v1/dashboard")
    app.state.settings = SimpleNamespace(
        context_docs_bucket=bucket, aws_region="us-east-1",
    )

    def _session_override() -> Iterator[_StubSession]:
        yield session

    app.dependency_overrides[get_session] = _session_override
    return app


def _row(doc_path: str, *, created_at: datetime, version: int = 1) -> SimpleNamespace:
    """Build a fake ``RepoContextDocRow``-shaped object — only the
    attributes the handler reads (``doc_path``, ``created_at``) need to
    be present; ``version`` is included for realism."""
    return SimpleNamespace(
        doc_path=doc_path, version=version, created_at=created_at,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Happy path ────────────────────────────────────────────────────────────────


def test_repo_docs_happy_path_arch_and_plans_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repo with one ``arch.md`` doc and three ``plans/*`` docs returns
    the typed ``RepoDocs`` shape — payload keys + values match
    ``src/api/types.ts`` ``RepoDocs`` and ``last_updated`` is the most
    recent doc's timestamp."""
    base = _now()
    rows = [
        _row(".treadmill/arch.md",       created_at=base - timedelta(hours=6)),
        _row("plans/2026-05-22-auth.md", created_at=base - timedelta(hours=4)),
        _row("plans/2026-05-24-bg.md",   created_at=base - timedelta(hours=2)),
        _row("plans/2026-05-25-perf.md", created_at=base - timedelta(minutes=15)),
    ]
    most_recent = base - timedelta(minutes=15)

    list_repo_docs = AsyncMock(return_value=rows)
    monkeypatch.setattr(
        repo_docs_mod,
        "OnboardingStore",
        lambda: SimpleNamespace(list_repo_docs=list_repo_docs),
    )

    app = _build_app(_StubSession())
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/dashboard/repos/treadmill/core/docs",
        )

    assert response.status_code == 200, response.text
    body = response.json()
    # Top-level keys match the RepoDocs interface 1:1.
    assert set(body) == {"arch", "plans", "last_updated"}
    assert body["arch"] == ".treadmill/arch.md"
    assert body["plans"] == 3
    # ``last_updated`` round-trips as ISO-8601; parse and compare to
    # avoid pinning whichever ``+00:00`` / ``Z`` form pydantic emits.
    parsed = datetime.fromisoformat(body["last_updated"].replace("Z", "+00:00"))
    assert parsed == most_recent
    # The accessor was invoked with the full ``owner/name`` repo path
    # (the ``{repo:path}`` matcher must not split on ``/``).
    list_repo_docs.assert_awaited_once()
    args = list_repo_docs.await_args.args
    assert args[1] == "treadmill/core"


def test_repo_docs_no_arch_returns_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repo with plan docs but no ``arch.md`` still returns 200 — the
    ``arch`` field is the empty string (presence indicator), not null,
    so the response remains typed against ``RepoDocs.arch: string``."""
    base = _now()
    rows = [
        _row("plans/2026-05-22-auth.md", created_at=base - timedelta(hours=1)),
    ]
    monkeypatch.setattr(
        repo_docs_mod,
        "OnboardingStore",
        lambda: SimpleNamespace(list_repo_docs=AsyncMock(return_value=rows)),
    )

    app = _build_app(_StubSession())
    with TestClient(app) as client:
        response = client.get("/api/v1/dashboard/repos/owner/name/docs")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["arch"] == ""
    assert body["plans"] == 1


# ── 404 — no docs indexed ─────────────────────────────────────────────────────


def test_repo_docs_404_when_no_docs_for_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0054 "absent data → 404, not empty payload": a repo whose
    index is empty must 404, never a zeroed-out RepoDocs payload."""
    monkeypatch.setattr(
        repo_docs_mod,
        "OnboardingStore",
        lambda: SimpleNamespace(list_repo_docs=AsyncMock(return_value=[])),
    )

    app = _build_app(_StubSession())
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/dashboard/repos/owner/never-onboarded/docs",
        )

    assert response.status_code == 404, response.text


# ── 503 — bucket unconfigured ────────────────────────────────────────────────


def test_repo_docs_503_when_context_docs_bucket_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CONTEXT_DOCS_BUCKET`` unset → shared ``get_context_store``
    dependency raises 503 before the handler runs — same shape as
    ``routers/context_docs.py``."""
    # The handler shouldn't be reached, but patch defensively so a
    # regression surfaces as 200 (loud) rather than a confusing crash.
    monkeypatch.setattr(
        repo_docs_mod,
        "OnboardingStore",
        lambda: SimpleNamespace(list_repo_docs=AsyncMock(return_value=[])),
    )

    app = _build_app(_StubSession(), bucket=None)
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/dashboard/repos/owner/name/docs",
        )

    assert response.status_code == 503, response.text
