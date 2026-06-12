"""Unit tests for the ADR-0090 CI-observer (task 5dd4a32d).

Fixtures are CAPTURED, not synthetic: real check-run and check-suite
objects from this repo's own CI, fetched via the GitHub API on
2026-06-12 —

* ``mixed_failure_*``: commit ``784e851`` (the #314 gate-bite proof) —
  one github-actions suite, 13 runs, 12 success + 1 failure (``cli``),
  GitHub's own suite rollup = ``failure``. Also carries the repo's REAL
  netlify suite parked at ``queued``/``None``.
* ``clean_success_*``: PR #335's head ``762f708`` — 13/13 success,
  suite rollup ``success`` (plus the same eternal netlify queue).

One aspect is necessarily reconstructed (and documented here): GitHub
does not let you re-fetch historical WEBHOOK bodies, so the per-delivery
envelope is rebuilt around each captured run — deliveries before the
suite's last completed run carry the suite snapshot as
``in_progress``/``None``, the final delivery carries the captured
suite's real ``completed``/conclusion. That progression is exactly
GitHub's documented delivery semantics; every payload FIELD the observer
reads is captured, not invented.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from treadmill_api.ci_observer import (
    SuiteCompletion,
    maybe_emit_ci_result,
    suite_completion_from_payload,
)
from treadmill_api.webhooks.normalize import normalize_github_event

FIXTURES = Path(__file__).parent / "fixtures" / "check_suites"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _suite_for(suites: dict, app_slug: str) -> dict:
    return next(
        s for s in suites["check_suites"] if s["app"]["slug"] == app_slug
    )


def _deliveries(runs_doc: dict, suites_doc: dict, repo: str) -> list[dict]:
    """Rebuild the webhook delivery sequence for the github-actions suite:
    captured run objects, suite snapshot in_progress for all but the
    final delivery (see module docstring)."""
    suite = _suite_for(suites_doc, "github-actions")
    runs = [
        r for r in runs_doc["check_runs"]
        if r["check_suite"]["id"] == suite["id"]
    ]
    bodies = []
    for i, run in enumerate(runs):
        final = i == len(runs) - 1
        snapshot = {
            "id": suite["id"],
            "status": suite["status"] if final else "in_progress",
            "conclusion": suite["conclusion"] if final else None,
            "head_sha": suite["head_sha"],
        }
        bodies.append(
            {
                "action": "completed",
                "check_run": {**run, "check_suite": snapshot},
                "repository": {"full_name": repo},
            }
        )
    return bodies


# ── Detection over captured deliveries ───────────────────────────────


def test_mixed_suite_yields_exactly_one_completion_with_failure_rollup() -> None:
    """13 real deliveries (12 success + 1 cli failure): only the final
    one detects suite completion, and the rollup is GitHub's own
    ``failure`` — a mixed suite never reads success."""
    bodies = _deliveries(
        _load("mixed_failure_runs.json"),
        _load("mixed_failure_suites.json"),
        "joeLepper/treadmill",
    )
    assert len(bodies) == 13
    completions = []
    for body in bodies:
        normalized = normalize_github_event("check_run", body)
        assert normalized is not None
        completions.append(suite_completion_from_payload(normalized.payload))
    assert completions[:-1] == [None] * 12
    final = completions[-1]
    assert isinstance(final, SuiteCompletion)
    assert final.conclusion == "failure"
    assert final.app_slug == "github-actions"
    assert final.check_suite_id == 73572867858


def test_clean_suite_completion_is_success() -> None:
    bodies = _deliveries(
        _load("clean_success_runs.json"),
        _load("clean_success_suites.json"),
        "joeLepper/treadmill",
    )
    final = suite_completion_from_payload(
        normalize_github_event("check_run", bodies[-1]).payload
    )
    assert final is not None
    assert final.conclusion == "success"


def test_netlify_queued_suite_never_completes() -> None:
    """The netlify-vs-CI distinction, from the REAL captured netlify
    suite: parked at ``queued``/``None`` — a run delivery carrying that
    snapshot must never read as a completion."""
    suites = _load("mixed_failure_suites.json")
    netlify = _suite_for(suites, "netlify")
    assert (netlify["status"], netlify["conclusion"]) == ("queued", None)
    payload = {
        "repo": "joeLepper/treadmill",
        "pr_number": None,
        "check_name": "netlify/deploy-preview",
        "conclusion": "neutral",
        "head_sha": netlify["head_sha"],
        "check_suite_id": netlify["id"],
        "suite_status": netlify["status"],
        "suite_conclusion": netlify["conclusion"],
        "app_slug": "netlify",
    }
    assert suite_completion_from_payload(payload) is None


def test_legacy_payload_without_suite_snapshot_is_ignored() -> None:
    """Pre-2026-06-12 check_run events have no suite fields — the
    observer must treat them as non-completions, not crash."""
    assert suite_completion_from_payload(
        {
            "repo": "joeLepper/treadmill",
            "check_name": "cli",
            "conclusion": "success",
            "head_sha": "a" * 40,
        }
    ) is None


# ── maybe_emit_ci_result wiring (stub session) ───────────────────────


class _StubResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalars(self):
        return self

    def first(self) -> Any:
        return self._value


class _StubSession:
    """Queued answers per execute() call, in observer call order."""

    def __init__(self, answers: list[Any]) -> None:
        self._answers = list(answers)
        self.added: list[Any] = []
        self.committed = False

    async def execute(self, stmt: Any):  # noqa: ANN001
        return _StubResult(self._answers.pop(0))

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, obj: Any) -> None:
        pass


class _StubPublisher:
    def __init__(self) -> None:
        self.published: list[Any] = []

    async def publish(self, event: Any, typed: Any) -> None:
        self.published.append(typed)


def _completion_payload() -> dict:
    """The final mixed-suite delivery's normalized payload (captured)."""
    bodies = _deliveries(
        _load("mixed_failure_runs.json"),
        _load("mixed_failure_suites.json"),
        "joeLepper/treadmill",
    )
    return normalize_github_event("check_run", bodies[-1]).payload


@pytest.mark.anyio
async def test_emit_attributed_via_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    task = SimpleNamespace(id=uuid.uuid4())

    async def fake_resolver(session, repo, sha):  # noqa: ANN001
        return task

    monkeypatch.setattr(
        "treadmill_api.ci_observer.resolve_task_by_head_sha", fake_resolver,
    )
    session = _StubSession(answers=[None])  # idempotency miss
    publisher = _StubPublisher()

    event = await maybe_emit_ci_result(session, publisher, _completion_payload())

    assert event is not None
    assert session.committed
    (added,) = session.added
    assert added.entity_type == "task"
    assert added.action == "ci_result"
    assert added.task_id == task.id
    assert added.commit_sha == "784e851725df784896c8c3174579230c302583d4"
    (typed,) = publisher.published
    assert typed.conclusion == "failure"
    assert typed.check_suite_id == 73572867858


@pytest.mark.anyio
async def test_idempotency_existing_ci_result_suppresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_resolver(session, repo, sha):  # noqa: ANN001
        raise AssertionError("must short-circuit before attribution")

    monkeypatch.setattr(
        "treadmill_api.ci_observer.resolve_task_by_head_sha", fake_resolver,
    )
    session = _StubSession(answers=[uuid.uuid4()])  # idempotency HIT
    publisher = _StubPublisher()

    event = await maybe_emit_ci_result(session, publisher, _completion_payload())

    assert event is None
    assert not session.added
    assert not publisher.published


@pytest.mark.anyio
async def test_unattributable_suite_emits_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_resolver(session, repo, sha):  # noqa: ANN001
        return None

    monkeypatch.setattr(
        "treadmill_api.ci_observer.resolve_task_by_head_sha", fake_resolver,
    )
    # idempotency miss, then events-join fallback misses too.
    session = _StubSession(answers=[None, None])
    publisher = _StubPublisher()

    event = await maybe_emit_ci_result(session, publisher, _completion_payload())

    assert event is None
    assert not session.added


@pytest.mark.anyio
async def test_fallback_events_join_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pr_opened-vs-registration race: resolver misses (head_sha
    never written), the events-join fallback lands the task."""

    async def fake_resolver(session, repo, sha):  # noqa: ANN001
        return None

    monkeypatch.setattr(
        "treadmill_api.ci_observer.resolve_task_by_head_sha", fake_resolver,
    )
    task_id = uuid.uuid4()
    # idempotency miss; fallback event-join returns pr_number; task_prs hit.
    session = _StubSession(answers=[None, "314", task_id])
    publisher = _StubPublisher()

    event = await maybe_emit_ci_result(session, publisher, _completion_payload())

    assert event is not None
    assert session.added[0].task_id == task_id


@pytest.mark.anyio
async def test_observer_never_raises_into_ingest() -> None:
    class _ExplodingSession:
        async def execute(self, stmt: Any):  # noqa: ANN001
            raise RuntimeError("db down")

    event = await maybe_emit_ci_result(
        _ExplodingSession(), _StubPublisher(), _completion_payload(),
    )
    assert event is None  # swallowed + logged; ingest unharmed


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
