"""Unit tests for the plan-merge-to-main trigger handler (ADR-0021).

Drives ``handle_pr_merged`` against a fake GitHub client + a stub
sessionmaker + a recording dispatcher / plan-creator. Proves the happy
path, the inactive-status branch, the parse-failure branch, the
no-plan-doc-files branch, the multi-plan-doc branch, and the
allow-list check.

The integration suite (``test_integration_plan_doc_merge.py``) covers
the end-to-end pipeline against live Postgres.
"""

from __future__ import annotations

import base64
import uuid
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from treadmill_api.coordination import plan_doc_trigger as pdt_mod
from treadmill_api.coordination.plan_doc_trigger import (
    _extract_frontmatter,
    _is_plan_doc_path,
    derive_plan_id,
    handle_pr_merged,
)


# ── Fixtures: fake GitHub client, stub session, recording plan creator ──────


class _FakeResponse:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        return self._body


class _FakeGithubClient:
    """Records every GET call + serves pre-queued responses per URL."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.responses: dict[str, list[_FakeResponse]] = {}

    def queue(self, url: str, response: _FakeResponse) -> None:
        self.responses.setdefault(url, []).append(response)

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        self.calls.append((url, params))
        queue = self.responses.get(url, [])
        if not queue:
            raise AssertionError(
                f"_FakeGithubClient: no response queued for {url} "
                f"(have: {list(self.responses.keys())})"
            )
        return queue.pop(0)


class _StubSession:
    """Records all execute / add / commit / rollback calls.

    Configured to return an "existing plan id = None" by default for the
    idempotency probe (so dispatch fires); tests can override
    ``execute_scalar_result`` to simulate a pre-existing Plan.
    """

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.commit = AsyncMock()
        self.rollback = AsyncMock()
        self.flush = AsyncMock()
        self.execute_calls: list[Any] = []
        self.execute_scalar_result: Any = None

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def execute(self, stmt: Any) -> Any:
        self.execute_calls.append(stmt)
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(
            return_value=self.execute_scalar_result,
        )
        return result


def _stub_sessionmaker(session: _StubSession) -> Any:
    @asynccontextmanager
    async def _cm() -> Any:
        yield session

    def _make() -> Any:
        return _cm()

    return _make


class _MultiSessionFactory:
    """Returns a fresh ``_StubSession`` per ``__call__``; tests can
    inspect ``self.sessions`` afterwards to assert per-session work."""

    def __init__(self) -> None:
        self.sessions: list[_StubSession] = []

    def __call__(self) -> Any:
        session = _StubSession()
        self.sessions.append(session)

        @asynccontextmanager
        async def _cm() -> Any:
            yield session

        return _cm()


class _StubSettings:
    def __init__(self, *, allowlist: set[str] | None = None) -> None:
        self._allowlist = allowlist or set()

    def plan_merge_repo_is_allowed(self, repo: str) -> bool:
        return not self._allowlist or repo in self._allowlist


# ── Plan-doc templates ──────────────────────────────────────────────────────


_VALID_ACTIVE_DOC = """---
status: active
trigger: test
---

# Plan: Test plan

## sequence_of_work

```yaml
sequence_of_work:
  - id: t0
    title: "First task"
    workflow: wf-author
    intent: First task description
    scope:
      files: [a.py]
    validation:
      - kind: deterministic
        description: "tests pass"
```
"""

_VALID_DRAFTING_DOC = """---
status: drafting
trigger: still working on it
---

# Plan: Drafting

## sequence_of_work

```yaml
sequence_of_work:
  - id: t0
    title: "First task"
    workflow: wf-author
    intent: First task description
    scope:
      files: [a.py]
    validation:
      - kind: deterministic
        description: "tests pass"
```
"""


_MALFORMED_DOC = """---
status: active
---

# Plan: Broken

## sequence_of_work

```yaml
sequence_of_work:
  - id: t0
    title: "x"
    # missing workflow + intent + scope + validation entirely
```
"""


_DOC_NO_FRONTMATTER = """# Plan: Untouched legacy plan

- **Status:** active
- **Date:** 2026-05-08

## sequence_of_work

```yaml
sequence_of_work:
  - id: t0
    title: "First task"
    workflow: wf-author
    intent: First task description
    scope:
      files: [a.py]
    validation:
      - kind: deterministic
        description: "tests pass"
```
"""


def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _files_response(*paths: str) -> _FakeResponse:
    """Build a /pulls/{n}/files response with the given file paths."""
    return _FakeResponse(
        200,
        [
            {"filename": p, "status": "modified", "changes": 10}
            for p in paths
        ],
    )


def _contents_response(content: str) -> _FakeResponse:
    return _FakeResponse(
        200,
        {"encoding": "base64", "content": _b64(content)},
    )


# ── _is_plan_doc_path ───────────────────────────────────────────────────────


def test_is_plan_doc_path_accepts_top_level():
    assert _is_plan_doc_path("docs/plans/2026-05-12-foo.md") is True


def test_is_plan_doc_path_rejects_nested():
    assert _is_plan_doc_path("docs/plans/2026/foo.md") is False


def test_is_plan_doc_path_rejects_non_md():
    assert _is_plan_doc_path("docs/plans/foo.txt") is False


def test_is_plan_doc_path_rejects_other_dirs():
    assert _is_plan_doc_path("docs/adrs/0021.md") is False
    assert _is_plan_doc_path("README.md") is False


# ── derive_plan_id ──────────────────────────────────────────────────────────


def test_derive_plan_id_is_deterministic():
    a = derive_plan_id("o/r", "docs/plans/x.md", "abc")
    b = derive_plan_id("o/r", "docs/plans/x.md", "abc")
    assert a == b


def test_derive_plan_id_differs_across_inputs():
    a = derive_plan_id("o/r", "docs/plans/x.md", "abc")
    b = derive_plan_id("o/r", "docs/plans/x.md", "def")
    c = derive_plan_id("o/r", "docs/plans/y.md", "abc")
    assert a != b
    assert a != c
    assert b != c


# ── _extract_frontmatter ────────────────────────────────────────────────────


def test_extract_frontmatter_parses_status():
    fm = _extract_frontmatter(_VALID_ACTIVE_DOC)
    assert fm is not None
    assert fm["status"] == "active"


def test_extract_frontmatter_returns_none_for_legacy_bullet_status():
    fm = _extract_frontmatter(_DOC_NO_FRONTMATTER)
    assert fm is None


def test_extract_frontmatter_returns_none_for_empty_doc():
    assert _extract_frontmatter("") is None


def test_extract_frontmatter_raises_on_malformed_yaml():
    import yaml as _yaml

    doc = "---\n: : :\n[invalid\n---\n# body\n"
    with pytest.raises(_yaml.YAMLError):
        _extract_frontmatter(doc)


# ── handle_pr_merged: happy path ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_pr_merged_happy_path_calls_create_plan_from_doc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PR with one ``docs/plans/foo.md`` whose frontmatter ``status:``
    is ``active`` triggers a Plan create via the internal function."""
    gh = _FakeGithubClient()
    gh.queue(
        "/repos/o/r/pulls/17/files",
        _files_response("docs/plans/2026-05-12-foo.md"),
    )
    gh.queue(
        "/repos/o/r/contents/docs/plans/2026-05-12-foo.md",
        _contents_response(_VALID_ACTIVE_DOC),
    )

    sm = _MultiSessionFactory()
    creator = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))
    monkeypatch.setattr(
        "treadmill_api.routers.plans.create_plan_from_doc", creator,
    )

    dispatched = await handle_pr_merged(
        sessionmaker=sm,
        dispatcher=MagicMock(),
        github_client=gh,
        settings=_StubSettings(),
        repo="o/r",
        pr_number=17,
        merge_commit_sha="cafe" * 10,
        sender="alice",
    )

    assert dispatched == 1
    creator.assert_awaited_once()
    call = creator.await_args
    assert call.kwargs["repo"] == "o/r"
    assert call.kwargs["doc_path"] == "docs/plans/2026-05-12-foo.md"
    assert call.kwargs["created_by"] == "alice"
    expected_plan_id = derive_plan_id(
        "o/r", "docs/plans/2026-05-12-foo.md", "cafe" * 10,
    )
    assert call.kwargs["plan_id"] == expected_plan_id


# ── handle_pr_merged: inactive status ───────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_pr_merged_drafting_persists_observed_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plan-doc that merged with ``status: drafting`` persists a
    ``plan_doc.observed_inactive`` event and skips dispatch."""
    gh = _FakeGithubClient()
    gh.queue(
        "/repos/o/r/pulls/9/files",
        _files_response("docs/plans/drafting.md"),
    )
    gh.queue(
        "/repos/o/r/contents/docs/plans/drafting.md",
        _contents_response(_VALID_DRAFTING_DOC),
    )

    sm = _MultiSessionFactory()
    creator = AsyncMock()
    monkeypatch.setattr(
        "treadmill_api.routers.plans.create_plan_from_doc", creator,
    )

    dispatched = await handle_pr_merged(
        sessionmaker=sm,
        dispatcher=MagicMock(),
        github_client=gh,
        settings=_StubSettings(),
        repo="o/r",
        pr_number=9,
        merge_commit_sha="bead" * 10,
        sender="bob",
    )

    assert dispatched == 0
    creator.assert_not_awaited()
    # Exactly one session opened to write the observed_inactive event.
    assert len(sm.sessions) == 1
    session = sm.sessions[0]
    assert len(session.added) == 1
    event = session.added[0]
    assert event.entity_type == "plan_doc"
    assert event.action == "observed_inactive"
    assert event.payload["status"] == "drafting"
    assert event.payload["repo"] == "o/r"
    assert event.payload["path"] == "docs/plans/drafting.md"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_pr_merged_no_frontmatter_persists_observed_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plan-doc with no ``---`` frontmatter is treated as inactive
    (no explicit ``status: active`` marker). The handler persists an
    observed_inactive event with ``status=None``."""
    gh = _FakeGithubClient()
    gh.queue(
        "/repos/o/r/pulls/3/files",
        _files_response("docs/plans/legacy.md"),
    )
    gh.queue(
        "/repos/o/r/contents/docs/plans/legacy.md",
        _contents_response(_DOC_NO_FRONTMATTER),
    )

    sm = _MultiSessionFactory()
    creator = AsyncMock()
    monkeypatch.setattr(
        "treadmill_api.routers.plans.create_plan_from_doc", creator,
    )

    dispatched = await handle_pr_merged(
        sessionmaker=sm,
        dispatcher=MagicMock(),
        github_client=gh,
        settings=_StubSettings(),
        repo="o/r",
        pr_number=3,
        merge_commit_sha="feed" * 10,
        sender="carol",
    )

    assert dispatched == 0
    creator.assert_not_awaited()
    assert len(sm.sessions) == 1
    event = sm.sessions[0].added[0]
    assert event.entity_type == "plan_doc"
    assert event.action == "observed_inactive"
    assert event.payload["status"] is None


# ── handle_pr_merged: parse failure ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_pr_merged_parse_failure_persists_parse_failed_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``status: active`` plan-doc whose body fails ``parse_plan_doc``
    persists a ``plan_doc.parse_failed`` event and skips dispatch.
    The consumer doesn't crash."""
    gh = _FakeGithubClient()
    gh.queue(
        "/repos/o/r/pulls/5/files",
        _files_response("docs/plans/broken.md"),
    )
    gh.queue(
        "/repos/o/r/contents/docs/plans/broken.md",
        _contents_response(_MALFORMED_DOC),
    )

    sm = _MultiSessionFactory()
    # Real create_plan_from_doc — it should raise ValidationError /
    # PlanDocFormatError, which the handler catches.
    dispatched = await handle_pr_merged(
        sessionmaker=sm,
        dispatcher=MagicMock(),
        github_client=gh,
        settings=_StubSettings(),
        repo="o/r",
        pr_number=5,
        merge_commit_sha="abad" * 10,
        sender="dan",
    )

    assert dispatched == 0
    # Two sessions: one to attempt the create (rolled back), one to
    # persist the parse_failed event.
    assert len(sm.sessions) >= 1
    # The last session opened is the one that persisted parse_failed.
    persisted = [
        added for s in sm.sessions for added in s.added
        if hasattr(added, "entity_type")
        and added.entity_type == "plan_doc"
        and added.action == "parse_failed"
    ]
    assert len(persisted) == 1
    event = persisted[0]
    assert event.payload["repo"] == "o/r"
    assert event.payload["path"] == "docs/plans/broken.md"
    assert event.payload["pr_number"] == 5
    # ``error_type`` is the exception class name; for a missing-field
    # malformed doc we'll see either ValidationError or
    # PlanDocFormatError. Both are acceptable here.
    assert event.payload["error_type"] in (
        "ValidationError", "PlanDocFormatError",
    )
    assert event.payload["error"]


# ── handle_pr_merged: no plan-doc files ─────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_pr_merged_skips_when_no_plan_doc_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PR that touches source code / ADRs / tests but no plan-doc
    files yields no dispatch and no events."""
    gh = _FakeGithubClient()
    gh.queue(
        "/repos/o/r/pulls/12/files",
        _files_response(
            "src/main.py",
            "docs/adrs/0021.md",
            "tests/test_x.py",
        ),
    )

    sm = _MultiSessionFactory()
    creator = AsyncMock()
    monkeypatch.setattr(
        "treadmill_api.routers.plans.create_plan_from_doc", creator,
    )

    dispatched = await handle_pr_merged(
        sessionmaker=sm,
        dispatcher=MagicMock(),
        github_client=gh,
        settings=_StubSettings(),
        repo="o/r",
        pr_number=12,
        merge_commit_sha="b001" * 10,
        sender="eve",
    )

    assert dispatched == 0
    creator.assert_not_awaited()
    # No sessions opened — no events to persist.
    assert sm.sessions == []
    # Only the list-files endpoint was hit.
    assert len(gh.calls) == 1
    assert gh.calls[0][0] == "/repos/o/r/pulls/12/files"


# ── handle_pr_merged: multiple plan docs in one PR ──────────────────────────


@pytest.mark.asyncio
async def test_handle_pr_merged_multiple_plan_docs_creates_multiple_plans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0021 Q21.b: one event per plan-doc; each processed
    independently. Two active plan docs → two Plan creations."""
    gh = _FakeGithubClient()
    gh.queue(
        "/repos/o/r/pulls/20/files",
        _files_response(
            "docs/plans/a.md",
            "docs/plans/b.md",
            "src/unrelated.py",
        ),
    )
    gh.queue(
        "/repos/o/r/contents/docs/plans/a.md",
        _contents_response(_VALID_ACTIVE_DOC),
    )
    gh.queue(
        "/repos/o/r/contents/docs/plans/b.md",
        _contents_response(_VALID_ACTIVE_DOC),
    )

    sm = _MultiSessionFactory()
    creator = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))
    monkeypatch.setattr(
        "treadmill_api.routers.plans.create_plan_from_doc", creator,
    )

    dispatched = await handle_pr_merged(
        sessionmaker=sm,
        dispatcher=MagicMock(),
        github_client=gh,
        settings=_StubSettings(),
        repo="o/r",
        pr_number=20,
        merge_commit_sha="feed" * 10,
        sender="frank",
    )

    assert dispatched == 2
    assert creator.await_count == 2
    # Each call carries its own plan_id (derived from path) — they must
    # differ.
    paths = {call.kwargs["doc_path"] for call in creator.await_args_list}
    assert paths == {"docs/plans/a.md", "docs/plans/b.md"}
    plan_ids = {call.kwargs["plan_id"] for call in creator.await_args_list}
    assert len(plan_ids) == 2


# ── handle_pr_merged: allow-list ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_pr_merged_skips_when_repo_not_in_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per ADR-0021 §"Per-repo enablement", a non-allow-listed repo
    short-circuits the handler — no GitHub calls, no events."""
    gh = _FakeGithubClient()
    sm = _MultiSessionFactory()
    creator = AsyncMock()
    monkeypatch.setattr(
        "treadmill_api.routers.plans.create_plan_from_doc", creator,
    )

    dispatched = await handle_pr_merged(
        sessionmaker=sm,
        dispatcher=MagicMock(),
        github_client=gh,
        settings=_StubSettings(allowlist={"only/allowed"}),
        repo="o/r",
        pr_number=7,
        merge_commit_sha="cafe" * 10,
        sender="grace",
    )

    assert dispatched == 0
    creator.assert_not_awaited()
    assert gh.calls == []
    assert sm.sessions == []


@pytest.mark.asyncio
async def test_handle_pr_merged_allowlisted_repo_proceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the repo IS in the allow-list the handler proceeds normally."""
    gh = _FakeGithubClient()
    gh.queue(
        "/repos/joe/treadmill/pulls/1/files",
        _files_response("docs/plans/x.md"),
    )
    gh.queue(
        "/repos/joe/treadmill/contents/docs/plans/x.md",
        _contents_response(_VALID_ACTIVE_DOC),
    )

    sm = _MultiSessionFactory()
    creator = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))
    monkeypatch.setattr(
        "treadmill_api.routers.plans.create_plan_from_doc", creator,
    )

    dispatched = await handle_pr_merged(
        sessionmaker=sm,
        dispatcher=MagicMock(),
        github_client=gh,
        settings=_StubSettings(allowlist={"joe/treadmill"}),
        repo="joe/treadmill",
        pr_number=1,
        merge_commit_sha="deef" * 10,
        sender="joe",
    )

    assert dispatched == 1
    creator.assert_awaited_once()


# ── handle_pr_merged: dependency wiring ─────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_pr_merged_no_github_client_short_circuits() -> None:
    """``github_client is None`` (e.g. GITHUB_TOKEN unset at boot) skips
    the handler cleanly — log + return."""
    dispatched = await handle_pr_merged(
        sessionmaker=_MultiSessionFactory(),
        dispatcher=MagicMock(),
        github_client=None,
        settings=_StubSettings(),
        repo="o/r",
        pr_number=1,
        merge_commit_sha="abcd" * 10,
        sender="ivy",
    )
    assert dispatched == 0


@pytest.mark.asyncio
async def test_handle_pr_merged_missing_merge_commit_sha_short_circuits() -> None:
    """No merge_commit_sha → no ref to fetch the doc at → no dispatch."""
    gh = _FakeGithubClient()
    dispatched = await handle_pr_merged(
        sessionmaker=_MultiSessionFactory(),
        dispatcher=MagicMock(),
        github_client=gh,
        settings=_StubSettings(),
        repo="o/r",
        pr_number=1,
        merge_commit_sha=None,
        sender="jay",
    )
    assert dispatched == 0
    # No GitHub calls were made.
    assert gh.calls == []


# ── handle_pr_merged: idempotency on SQS redelivery ─────────────────────────


@pytest.mark.asyncio
async def test_handle_pr_merged_skips_when_plan_already_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQS redelivery of the same pr_merged event must converge on the
    same Plan row. The handler probes for an existing Plan via the
    deterministic plan_id and skips dispatch when found."""
    gh = _FakeGithubClient()
    gh.queue(
        "/repos/o/r/pulls/17/files",
        _files_response("docs/plans/foo.md"),
    )
    gh.queue(
        "/repos/o/r/contents/docs/plans/foo.md",
        _contents_response(_VALID_ACTIVE_DOC),
    )

    # The sessionmaker yields a session whose existence-probe returns a
    # pre-existing plan_id.
    existing_plan_id = derive_plan_id(
        "o/r", "docs/plans/foo.md", "cafe" * 10,
    )
    sessions: list[_StubSession] = []

    def _factory():
        s = _StubSession()
        s.execute_scalar_result = existing_plan_id
        sessions.append(s)

        @asynccontextmanager
        async def _cm():
            yield s

        return _cm()

    creator = AsyncMock()
    monkeypatch.setattr(
        "treadmill_api.routers.plans.create_plan_from_doc", creator,
    )

    dispatched = await handle_pr_merged(
        sessionmaker=_factory,
        dispatcher=MagicMock(),
        github_client=gh,
        settings=_StubSettings(),
        repo="o/r",
        pr_number=17,
        merge_commit_sha="cafe" * 10,
        sender="kay",
    )

    assert dispatched == 0
    creator.assert_not_awaited()
