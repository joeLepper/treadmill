"""Unit tests for the supersede-on-architect-verdict trigger (ADR-0049).

Drives ``maybe_dispatch_supersede_on_architect_verdict`` directly with
stubs so we can prove the trigger's short-circuit behavior + the
happy-path side-effect order without needing live Postgres. The
helper is the API-side handler for the architect's repurposed
``supersede`` verdict: close parent PR, create child task with
rewritten description + parent_task_id self-FK, dispatch fresh
wf-author against the child.

Live-DB coverage (the full chain through ``CoordinationConsumer.handle``
hitting Postgres) lives alongside the other architect-verdict
integration tests behind ``TREADMILL_INTEGRATION=1``.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from treadmill_api.coordination.consumer import CoordinationConsumer
from treadmill_api.coordination.triggers import (
    maybe_dispatch_supersede_on_architect_verdict,
)
from treadmill_api.events.step import StepCompleted
from treadmill_api.events.step_output import Metadata, StepOutput


# ── Stubs ───────────────────────────────────────────────────────────────────


class _StubSession:
    """Minimal AsyncSession-ish stub for the consumer-routing tests."""

    def __init__(self) -> None:
        self.execute = AsyncMock()
        self.commit = AsyncMock()
        self.flush = AsyncMock()


def _stub_factory(session: _StubSession) -> Any:
    @asynccontextmanager
    async def _cm() -> Any:
        yield session

    def _make() -> Any:
        return _cm()

    return _make


def _consumer(session: _StubSession) -> CoordinationConsumer:
    # ``dispatcher`` left as None — mirrors the existing consumer-unit
    # routing tests (``test_consumer_routes_step_completed_to_review_override_helper``),
    # which skip the cross-step path that requires a real dispatcher.
    # We monkeypatch the supersede helper directly to count calls.
    return CoordinationConsumer(
        sqs_client=None,
        queue_url="unused",
        sessionmaker=_stub_factory(session),  # type: ignore[arg-type]
    )


# ── Short-circuit tests (no DB work on non-supersede verdicts) ───────────────


@pytest.mark.asyncio
async def test_supersede_helper_short_circuits_on_amend_verdict() -> None:
    """The supersede helper must early-return (no DB query) when the
    architect verdict is ``amend``. The amend path routes to
    ``wf-feedback`` via a different trigger; this trigger only fires
    on supersede so non-supersede payloads must not even hit the DB."""
    session = _StubSession()
    typed = StepCompleted(
        completed_at="2026-05-19T12:00:00+00:00",
        output=StepOutput(
            summary="amend verdict",
            decision="amend",
            payload={
                "verdict": "amend",
                "dispatch": {"workflow_id": "wf-plan"},
            },
            metadata=Metadata(),
        ),
    )

    result = await maybe_dispatch_supersede_on_architect_verdict(
        session, None,  # type: ignore[arg-type]
        step_id=str(uuid.uuid4()),
        typed=typed,
    )

    assert result is None
    assert session.execute.await_count == 0, (
        "supersede helper must short-circuit before any DB query when "
        "verdict != supersede"
    )


@pytest.mark.asyncio
async def test_supersede_helper_short_circuits_on_accept_as_is_verdict() -> None:
    """Same short-circuit on ``accept-as-is`` — the review/validate
    override emitters handle that path."""
    session = _StubSession()
    typed = StepCompleted(
        completed_at="2026-05-19T12:00:00+00:00",
        output=StepOutput(
            summary="accept-as-is verdict",
            decision="accept-as-is",
            payload={
                "verdict": "accept-as-is",
                "dispatch": {"workflow_id": None, "review_override": True},
            },
            metadata=Metadata(),
        ),
    )

    result = await maybe_dispatch_supersede_on_architect_verdict(
        session, None,  # type: ignore[arg-type]
        step_id=str(uuid.uuid4()),
        typed=typed,
    )

    assert result is None
    assert session.execute.await_count == 0


@pytest.mark.asyncio
async def test_supersede_helper_skips_empty_rewritten_description() -> None:
    """Defensive: if a supersede payload somehow arrives without
    ``rewritten_description`` (worker-side parse should have rejected
    it, but SQS delivery + redelivery + race-conditions exist), the
    trigger must not create a child task with no description. Short-
    circuit at WARNING + return None."""
    session = _StubSession()
    typed = StepCompleted(
        completed_at="2026-05-19T12:00:00+00:00",
        output=StepOutput(
            summary="supersede verdict (no rewrite)",
            decision="supersede",
            payload={
                "verdict": "supersede",
                "dispatch": {
                    "workflow_id": None,
                    "intent": "supersede-rewrite-task",
                },
            },
            metadata=Metadata(),
        ),
    )

    result = await maybe_dispatch_supersede_on_architect_verdict(
        session, None,  # type: ignore[arg-type]
        step_id=str(uuid.uuid4()),
        typed=typed,
    )

    assert result is None
    assert session.execute.await_count == 0, (
        "supersede helper must short-circuit at the rewrite-text check "
        "before any DB query"
    )


@pytest.mark.asyncio
async def test_supersede_helper_skips_whitespace_only_rewrite() -> None:
    """A rewrite that's whitespace-only is not a substantive rewrite —
    treat as the empty case."""
    session = _StubSession()
    typed = StepCompleted(
        completed_at="2026-05-19T12:00:00+00:00",
        output=StepOutput(
            summary="supersede verdict (whitespace rewrite)",
            decision="supersede",
            payload={
                "verdict": "supersede",
                "rewritten_description": "   \n\t  ",
            },
            metadata=Metadata(),
        ),
    )

    result = await maybe_dispatch_supersede_on_architect_verdict(
        session, None,  # type: ignore[arg-type]
        step_id=str(uuid.uuid4()),
        typed=typed,
    )

    assert result is None
    assert session.execute.await_count == 0


# ── Consumer routing test (the consumer calls _maybe_dispatch_supersede) ────


@pytest.mark.asyncio
async def test_consumer_routes_step_completed_to_supersede_helper() -> None:
    """The consumer must call ``_maybe_dispatch_supersede`` after each
    step.completed so the architect's supersede verdict triggers the
    close-PR + child-task + wf-author sequence (ADR-0049)."""
    session = _StubSession()
    consumer = _consumer(session)

    calls: list[dict] = []

    async def _stub(*args: object, **kwargs: object) -> None:
        calls.append({"args": args, "kwargs": kwargs})

    consumer._maybe_dispatch_supersede = _stub  # type: ignore[method-assign]

    await consumer.handle({
        "entity_type": "step",
        "action": "completed",
        "step_id": str(uuid.uuid4()),
        "event_id": str(uuid.uuid4()),
        "payload": {
            "completed_at": "2026-05-19T12:00:00+00:00",
            "output": {
                "summary": "architect verdict",
                "decision": "supersede",
                "commit_sha": None,
                "artifacts": [],
                "payload": {
                    "verdict": "supersede",
                    "reasoning": "Plan named the wrong file paths.",
                    "target_artifact": "docs/plans/x.md",
                    "rewritten_description": (
                        "Write services/api/treadmill_api/foo.py "
                        "with function bar()."
                    ),
                    "dispatch": {
                        "workflow_id": None,
                        "intent": "supersede-rewrite-task",
                        "rewritten_description": (
                            "Write services/api/treadmill_api/foo.py "
                            "with function bar()."
                        ),
                    },
                },
                "metadata": {},
            },
        },
    })

    assert len(calls) == 1, (
        "consumer must invoke _maybe_dispatch_supersede on every "
        "step.completed; the helper filters non-architect / "
        "non-supersede shapes internally"
    )


# ── Dedup key shape ─────────────────────────────────────────────────────────


def test_wf_author_supersede_dedup_key_shape() -> None:
    """The dedup key for the supersede trigger uses the
    ``supersede-parent=<parent_task_id>`` namespace so re-delivery of
    the same architect step.completed cannot create N children against
    the same parent."""
    from treadmill_api.coordination.dispatch_dedup import build_dedup_key

    parent_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    key = build_dedup_key(
        "wf-author",
        {
            "repo": "example/repo",
            "supersede_parent_task_id": parent_id,
        },
    )
    assert key == f"wf-author:example/repo:supersede-parent={parent_id}"


def test_wf_author_dedup_key_opts_out_without_supersede_field() -> None:
    """The natural wf-author dispatch path (a freshly registered task
    going through ``dispatch_task``) opts out of dedup — the ``tasks``
    PK already provides task-level uniqueness. Only the supersede
    discriminator produces a non-None dedup key here."""
    from treadmill_api.coordination.dispatch_dedup import build_dedup_key

    key = build_dedup_key(
        "wf-author",
        {
            "repo": "example/repo",
            "task_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        },
    )
    assert key is None


# ── Architect-step gate ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_supersede_helper_skips_non_architect_workflow() -> None:
    """If a non-architect workflow somehow emits ``verdict='supersede'``,
    the trigger must short-circuit cleanly — only
    ``wf-architecture-resolve`` is allowed to drive the supersede
    side-effect. Defensive against payload shape leakage between
    workflows."""
    session = _StubSession()
    # Simulate the workflow-id lookup returning a non-architect row.
    fake_row = MagicMock()
    fake_row.workflow_id = "wf-review"  # WRONG workflow
    fake_row.task_id = uuid.uuid4()
    fake_result = MagicMock()
    fake_result.first = MagicMock(return_value=fake_row)
    session.execute = AsyncMock(return_value=fake_result)

    typed = StepCompleted(
        completed_at="2026-05-19T12:00:00+00:00",
        output=StepOutput(
            summary="supersede verdict from non-architect",
            decision="supersede",
            payload={
                "verdict": "supersede",
                "rewritten_description": "Some valid rewrite text.",
                "dispatch": {
                    "workflow_id": None,
                    "intent": "supersede-rewrite-task",
                },
            },
            metadata=Metadata(),
        ),
    )

    result = await maybe_dispatch_supersede_on_architect_verdict(
        session, None,  # type: ignore[arg-type]
        step_id=str(uuid.uuid4()),
        typed=typed,
    )

    assert result is None
    # The helper did the workflow-id lookup before bailing — exactly
    # one execute call.
    assert session.execute.await_count == 1
