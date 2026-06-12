"""Unit tests for the lazy pr_conflicting resolver (task 536bf319).

Root cause pinned here: the ``task_mergeability`` VIEW's
``pr_conflicting`` column reads ``github.pr_conflict`` events whose only
producer (the conflict-detection sweep) was deleted in ADR-0087 Phase 5
— so the column stayed NULL forever and coordinators burned their full
30×10s poll budget treating NULL as "GitHub still computing" while
``gh pr view`` said MERGEABLE.

``GET /tasks/{id}/mergeability`` now resolves lazily: NULL conflict
state at a known head → one GitHub REST call → persist the DEFINITIVE
answer (true OR false — false is the clean signal that never existed)
as the canonical event with ``commit_sha`` stamped for the VIEW's join,
then re-read the VIEW.

Coverage axes:
  * clean PR (mergeable=true) → pr_conflict(is_conflicting=False)
    persisted with commit_sha=head, VIEW re-read, response resolved
  * dirty PR (mergeable=false) → is_conflicting=True persisted
  * GitHub still computing (mergeable=null) → nothing persisted; the
    caller's next poll retries
  * stale head (a push raced the lookup) → nothing persisted
  * GitHub API failure → nothing persisted, endpoint still 200s
  * no github client configured → no resolution attempted
  * already-resolved column → GitHub never consulted (steady state is
    read-only)
  * task with no PR row → no resolution attempted
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from treadmill_api.dependencies_db import get_session
from treadmill_api.dispatch import get_dispatcher
from treadmill_api.routers.tasks import router as tasks_router


TASK_ID = uuid.uuid4()
HEAD = "a" * 40
REPO = "joeLepper/treadmill"


def _view_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "task_id": TASK_ID,
        "repo": REPO,
        "pr_number": 311,
        "head_sha": HEAD,
        "review_decision": None,
        "validate_decision": None,
        "ci_conclusion": "success",
        "pr_conflicting": None,
        "derived_mergeability": "pending",
    }
    base.update(overrides)
    return base


class _StubResult:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = SimpleNamespace(**row) if row is not None else None

    def one_or_none(self) -> Any | None:
        return self._row


class _StubSession:
    """Returns queued view rows in order — one per VIEW read."""

    def __init__(self, rows: list[dict[str, Any] | None]) -> None:
        self._rows = list(rows)
        self.reads = 0
        self.committed = False

    async def execute(self, statement: Any, params: Any = None) -> _StubResult:
        self.reads += 1
        return _StubResult(self._rows.pop(0))

    async def commit(self) -> None:
        self.committed = True


class _StubDispatcher:
    def __init__(self) -> None:
        self.persisted: list[dict[str, Any]] = []

    async def persist_and_publish(
        self, session: Any, **kwargs: Any
    ) -> Any:
        self.persisted.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())


class _StubGithubResponse:
    def __init__(self, status_code: int, body: dict[str, Any] | None = None):
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> dict[str, Any]:
        return self._body


class _StubGithubClient:
    def __init__(self, response: _StubGithubResponse | Exception) -> None:
        self._response = response
        self.calls: list[str] = []

    async def get(self, url: str) -> _StubGithubResponse:
        self.calls.append(url)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _RaisingGithubClient:
    """Proves the steady state never touches GitHub."""

    async def get(self, url: str) -> Any:
        raise AssertionError("GitHub must not be consulted")


def _app(
    session: _StubSession,
    dispatcher: _StubDispatcher,
    github_client: Any,
) -> FastAPI:
    app = FastAPI()
    app.include_router(tasks_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_dispatcher] = lambda: dispatcher
    app.state.github_client = github_client
    return app


def _get(app: FastAPI) -> Any:
    client = TestClient(app)
    return client.get(f"/api/v1/tasks/{TASK_ID}/mergeability")


def _github_pr(mergeable: bool | None, head_sha: str = HEAD) -> _StubGithubResponse:
    return _StubGithubResponse(
        200, {"mergeable": mergeable, "head": {"sha": head_sha}}
    )


def test_clean_pr_persists_false_and_resolves() -> None:
    """mergeable=true → is_conflicting=False persisted (the clean signal
    that never existed under the sweep), VIEW re-read, response resolved."""
    session = _StubSession([
        _view_row(),  # first read: NULL conflict
        _view_row(pr_conflicting=False, derived_mergeability="pending"),
    ])
    dispatcher = _StubDispatcher()
    github = _StubGithubClient(_github_pr(mergeable=True))

    resp = _get(_app(session, dispatcher, github))

    assert resp.status_code == 200, resp.text
    assert resp.json()["pr_conflicting"] is False
    assert session.reads == 2
    assert session.committed
    (persisted,) = dispatcher.persisted
    assert persisted["entity_type"] == "github"
    assert persisted["action"] == "pr_conflict"
    assert persisted["commit_sha"] == HEAD  # the VIEW's join key
    assert persisted["task_id"] == TASK_ID
    payload = persisted["payload"]
    assert payload.is_conflicting is False
    assert payload.repo == REPO
    assert payload.pr_number == 311
    assert payload.head_sha == HEAD
    assert github.calls == [f"/repos/{REPO}/pulls/311"]


def test_dirty_pr_persists_true() -> None:
    session = _StubSession([
        _view_row(),
        _view_row(pr_conflicting=True, derived_mergeability="blocked-on-conflict"),
    ])
    dispatcher = _StubDispatcher()
    github = _StubGithubClient(_github_pr(mergeable=False))

    resp = _get(_app(session, dispatcher, github))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pr_conflicting"] is True
    assert body["derived_mergeability"] == "blocked-on-conflict"
    assert dispatcher.persisted[0]["payload"].is_conflicting is True


def test_github_still_computing_persists_nothing() -> None:
    """mergeable=null → no event; the coordinator's next poll retries."""
    session = _StubSession([_view_row()])
    dispatcher = _StubDispatcher()
    github = _StubGithubClient(_github_pr(mergeable=None))

    resp = _get(_app(session, dispatcher, github))

    assert resp.status_code == 200, resp.text
    assert resp.json()["pr_conflicting"] is None
    assert not dispatcher.persisted
    assert session.reads == 1


def test_stale_head_persists_nothing() -> None:
    """A push raced the lookup: GitHub answers for a NEWER head — do not
    write a signal keyed to the old one."""
    session = _StubSession([_view_row()])
    dispatcher = _StubDispatcher()
    github = _StubGithubClient(_github_pr(mergeable=True, head_sha="b" * 40))

    resp = _get(_app(session, dispatcher, github))

    assert resp.status_code == 200, resp.text
    assert not dispatcher.persisted


def test_github_failure_never_500s() -> None:
    session = _StubSession([_view_row()])
    dispatcher = _StubDispatcher()
    github = _StubGithubClient(_StubGithubResponse(503))

    resp = _get(_app(session, dispatcher, github))

    assert resp.status_code == 200, resp.text
    assert resp.json()["pr_conflicting"] is None
    assert not dispatcher.persisted


def test_github_exception_never_500s() -> None:
    session = _StubSession([_view_row()])
    dispatcher = _StubDispatcher()
    github = _StubGithubClient(RuntimeError("network down"))

    resp = _get(_app(session, dispatcher, github))

    assert resp.status_code == 200, resp.text
    assert not dispatcher.persisted


def test_no_github_client_skips_resolution() -> None:
    session = _StubSession([_view_row()])
    dispatcher = _StubDispatcher()

    resp = _get(_app(session, dispatcher, github_client=None))

    assert resp.status_code == 200, resp.text
    assert resp.json()["pr_conflicting"] is None
    assert not dispatcher.persisted


def test_already_resolved_column_is_read_only() -> None:
    """Steady state: a non-NULL column short-circuits before GitHub."""
    session = _StubSession([
        _view_row(pr_conflicting=False, derived_mergeability="pending"),
    ])
    dispatcher = _StubDispatcher()

    resp = _get(_app(session, dispatcher, _RaisingGithubClient()))

    assert resp.status_code == 200, resp.text
    assert resp.json()["pr_conflicting"] is False
    assert session.reads == 1
    assert not dispatcher.persisted


def test_task_without_pr_skips_resolution() -> None:
    session = _StubSession([
        {
            "task_id": TASK_ID,
            "repo": None,
            "pr_number": None,
            "head_sha": None,
            "review_decision": None,
            "validate_decision": None,
            "ci_conclusion": None,
            "pr_conflicting": None,
            "derived_mergeability": None,
        }
    ])
    dispatcher = _StubDispatcher()

    resp = _get(_app(session, dispatcher, _RaisingGithubClient()))

    assert resp.status_code == 200, resp.text
    assert resp.json()["derived_mergeability"] == "pending"
    assert not dispatcher.persisted


class _MalformedBodyResponse:
    """200 whose body is not parseable/shaped as expected."""

    status_code = 200

    def __init__(self, mode: str) -> None:
        self._mode = mode

    def json(self) -> Any:
        if self._mode == "raises":
            raise ValueError("truncated JSON")
        return ["not", "a", "dict"]  # .get() raises AttributeError


def test_malformed_200_body_never_500s() -> None:
    """A 200 with an unparseable body must degrade like any other GitHub
    hiccup — no event, no 500 (PR #320 review blocking item; the #315
    lesson class: external-input shape failures inside the contract)."""
    for mode in ("raises", "non_dict"):
        session = _StubSession([_view_row()])
        dispatcher = _StubDispatcher()
        github = _StubGithubClient(_MalformedBodyResponse(mode))

        resp = _get(_app(session, dispatcher, github))

        assert resp.status_code == 200, (mode, resp.text)
        assert resp.json()["pr_conflicting"] is None
        assert not dispatcher.persisted
