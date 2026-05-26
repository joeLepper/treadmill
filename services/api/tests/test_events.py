"""Unit tests for the per-event-type Pydantic models + registry.

ADR-0011 requires that every (entity_type, action) pair Treadmill emits
or consumes have a typed payload class. These tests enforce the contract:
round-trips work, malformed payloads are rejected, and the registry stays
exhaustive.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from treadmill_api.events import (
    EVENT_REGISTRY,
    Artifact,
    EventPayload,
    GithubCheckRunCompleted,
    GithubPrConflict,
    GithubPrMerged,
    GithubPrOpened,
    GithubPrReviewSubmitted,
    GithubPrSynchronize,
    Metadata,
    PlanAbandoned,
    PlanActivated,
    PlanCompleted,
    PlanPlanningStarted,
    PlanRegistered,
    StepCancelled,
    StepCompleted,
    StepFailed,
    StepOutput,
    StepReady,
    StepStarted,
    TaskCancelled,
    TaskReady,
    TaskRegistered,
    UnknownEventTypeError,
    encode_payload,
    parse_payload,
)


# ── Round-trip tests for each payload type ────────────────────────────────────


def _round_trip(payload: EventPayload) -> EventPayload:
    encoded = encode_payload(payload)
    return parse_payload(payload.ENTITY_TYPE, payload.ACTION, encoded)


def test_task_registered_round_trip():
    original = TaskRegistered(
        repo="RAMJAC/treadmill",
        title="Add /health endpoint",
        workflow_version_id=uuid.uuid4(),
        plan_id=uuid.uuid4(),
    )
    parsed = _round_trip(original)
    assert isinstance(parsed, TaskRegistered)
    assert parsed == original


def test_task_cancelled_with_optional_reason():
    original = TaskCancelled(reason="superseded by t-42")
    parsed = _round_trip(original)
    assert parsed == original


def test_task_cancelled_with_no_reason():
    original = TaskCancelled()
    parsed = _round_trip(original)
    assert parsed.reason is None


def test_task_ready_has_no_required_fields():
    """TaskReady payload is empty; the entity it refers to lives on the
    Event row's task_id column."""
    original = TaskReady()
    parsed = _round_trip(original)
    assert isinstance(parsed, TaskReady)


def test_plan_registered_round_trip():
    original = PlanRegistered(repo="RAMJAC/treadmill", intent="Add billing")
    parsed = _round_trip(original)
    assert parsed == original


def test_plan_planning_started_round_trip():
    original = PlanPlanningStarted(workflow_version_id=uuid.uuid4())
    parsed = _round_trip(original)
    assert parsed == original


def test_plan_activated_round_trip():
    original = PlanActivated(doc_path="docs/plans/2026-05-08-billing.md")
    parsed = _round_trip(original)
    assert parsed == original


def test_plan_completed_round_trip():
    original = PlanCompleted()
    parsed = _round_trip(original)
    assert parsed == original


def test_plan_abandoned_round_trip():
    original = PlanAbandoned(reason="redirected by user")
    parsed = _round_trip(original)
    assert parsed == original


def test_step_ready_round_trip():
    original = StepReady(
        role_id="role-author",
        step_index=0,
        step_name="author",
        repo="RAMJAC/treadmill",
        workflow_id="wf-author",
    )
    parsed = _round_trip(original)
    assert parsed == original


def test_step_ready_does_not_carry_compute_tier_at_v0():
    """Per decision #12 in the 2026-05-11 closure plan: ``compute_tier``
    is removed from the wire (StepReady, worker decoder, steps-router
    response). The DB column stays as forward-compat ballast for the
    future multi-tier ADR. Verify it's not on the StepReady event payload."""
    payload = StepReady(
        role_id="role-author",
        step_index=0,
        step_name="author",
        repo="RAMJAC/treadmill",
        workflow_id="wf-author",
    )
    dumped = payload.model_dump(mode="json")
    assert "compute_tier" not in dumped

    # Sending one over the wire must also be rejected as an extra field.
    with pytest.raises(ValidationError):
        StepReady.model_validate(
            {
                "role_id": "role-author",
                "step_index": 0,
                "step_name": "author",
                "repo": "RAMJAC/treadmill",
                "workflow_id": "wf-author",
                "compute_tier": "standard",
            }
        )


def test_step_started_round_trip():
    original = StepStarted(started_at=datetime.now(timezone.utc))
    parsed = _round_trip(original)
    # datetime equality after JSON round-trip — Pydantic preserves TZ.
    assert parsed.started_at == original.started_at


def test_step_completed_round_trip():
    """Per ADR-0012, ``StepCompleted.output`` is a uniform ``StepOutput``
    envelope. Round-trip the wire form to prove the consumer reads back
    the same envelope the worker writes."""
    original = StepCompleted(
        completed_at=datetime.now(timezone.utc),
        output=StepOutput(
            summary="branch pushed",
            decision="pushed",
            commit_sha="deadbeef" * 5,
            artifacts=[Artifact(kind="branch", value="task/abc-feat")],
            payload={"pr_number": 42},
            metadata=Metadata(),
        ),
    )
    parsed = _round_trip(original)
    assert parsed.completed_at == original.completed_at
    assert isinstance(parsed.output, StepOutput)
    assert parsed.output == original.output
    # ADR-0020 Wave 1: token_usage defaults to None when the worker
    # didn't make an LLM call (dry-run, validation step).
    assert parsed.token_usage is None


def test_step_completed_with_token_usage_round_trip():
    """ADR-0020 Wave 1: ``StepCompleted.token_usage`` carries a typed
    ``StepTokenUsage`` sub-model that round-trips through the
    registry. The five counters + ``model`` survive JSON encoding."""
    from treadmill_api.events import StepTokenUsage

    original = StepCompleted(
        completed_at=datetime.now(timezone.utc),
        output=StepOutput(
            summary="did the thing",
            decision="pushed",
            artifacts=[],
            payload={},
            metadata=Metadata(),
        ),
        token_usage=StepTokenUsage(
            input_tokens=1200,
            output_tokens=340,
            cache_creation_tokens=50,
            cache_read_tokens=800,
            model="claude-opus-4-7",
        ),
    )
    parsed = _round_trip(original)
    assert isinstance(parsed, StepCompleted)
    assert parsed.token_usage is not None
    assert parsed.token_usage.input_tokens == 1200
    assert parsed.token_usage.output_tokens == 340
    assert parsed.token_usage.cache_creation_tokens == 50
    assert parsed.token_usage.cache_read_tokens == 800
    assert parsed.token_usage.model == "claude-opus-4-7"


def test_step_failed_round_trip():
    original = StepFailed(failed_at=datetime.now(timezone.utc), error="exit 1")
    parsed = _round_trip(original)
    assert parsed.error == "exit 1"


def test_step_cancelled_round_trip():
    original = StepCancelled(reason="task cancelled")
    parsed = _round_trip(original)
    assert parsed == original


def test_github_pr_opened_round_trip():
    original = GithubPrOpened(
        repo="RAMJAC/treadmill",
        pr_number=42,
        sender="alice",
        title="Add /health endpoint",
        head_branch="task/abc-feat",
        head_sha="deadbeef" * 5,
    )
    parsed = _round_trip(original)
    assert parsed == original


def test_github_pr_merged_round_trip():
    original = GithubPrMerged(
        repo="RAMJAC/treadmill",
        pr_number=42,
        sender="alice",
        merged_sha="cafebabe" * 5,
    )
    parsed = _round_trip(original)
    assert parsed == original


def test_github_pr_synchronize_round_trip():
    """Per ADR-0014, ``pr_synchronize`` carries the new HEAD SHA + the
    prior SHA so the mergeability VIEW can invalidate stale thumbs."""
    original = GithubPrSynchronize(
        repo="RAMJAC/treadmill",
        pr_number=42,
        sender="alice",
        head_sha="cafebabe" * 5,
        before_sha="deadbeef" * 5,
    )
    parsed = _round_trip(original)
    assert isinstance(parsed, GithubPrSynchronize)
    assert parsed == original


def test_github_pr_synchronize_before_sha_is_optional():
    """``before_sha`` may be ``None`` if GitHub omits the ``before`` field
    (e.g. force-push edge cases). The payload still validates."""
    original = GithubPrSynchronize(
        repo="RAMJAC/treadmill",
        pr_number=42,
        sender="alice",
        head_sha="cafebabe" * 5,
    )
    parsed = _round_trip(original)
    assert parsed.before_sha is None


def test_github_pr_synchronize_in_registry():
    """The registry maps ``(github, pr_synchronize)`` to
    ``GithubPrSynchronize``."""
    assert EVENT_REGISTRY[("github", "pr_synchronize")] is GithubPrSynchronize


def test_github_pr_conflict_round_trip():
    """Per ADR-0013, ``pr_conflict`` is the conflict signal for the
    mergeability VIEW. ``is_conflicting`` is the field name the VIEW
    filters on; the round-trip preserves it."""
    original = GithubPrConflict(
        repo="RAMJAC/treadmill",
        pr_number=42,
        head_sha="deadbeef" * 5,
        is_conflicting=True,
    )
    parsed = _round_trip(original)
    assert isinstance(parsed, GithubPrConflict)
    assert parsed == original
    assert parsed.is_conflicting is True


def test_github_pr_conflict_in_registry():
    """The registry maps ``(github, pr_conflict)`` to ``GithubPrConflict``."""
    assert EVENT_REGISTRY[("github", "pr_conflict")] is GithubPrConflict


def test_github_pr_review_submitted_round_trip():
    original = GithubPrReviewSubmitted(
        repo="RAMJAC/treadmill",
        pr_number=42,
        sender="bob",
        state="changes_requested",
        body="needs tests",
    )
    parsed = _round_trip(original)
    assert parsed == original


def test_github_check_run_completed_round_trip():
    original = GithubCheckRunCompleted(
        repo="RAMJAC/treadmill",
        pr_number=42,
        check_name="ci",
        conclusion="failure",
        head_sha="deadbeef" * 5,
    )
    parsed = _round_trip(original)
    assert parsed == original


# ── Strict validation: reject malformed payloads ──────────────────────────────


def test_extra_fields_are_rejected():
    """Per ADR-0011's strict-contract stance, unknown fields raise."""
    with pytest.raises(ValidationError):
        TaskCancelled.model_validate({"reason": "foo", "extra_field": "nope"})


def test_missing_required_field_raises():
    with pytest.raises(ValidationError):
        TaskRegistered.model_validate({"repo": "x", "title": "y"})  # missing ids


def test_wrong_type_raises():
    with pytest.raises(ValidationError):
        GithubPrOpened.model_validate(
            {
                "repo": "x",
                "pr_number": "not-an-int",  # wrong type
                "sender": "alice",
                "title": "t",
                "head_branch": "b",
                "head_sha": "s",
            }
        )


def test_parse_payload_rejects_unknown_event_type():
    with pytest.raises(UnknownEventTypeError) as exc_info:
        parse_payload("unknown_entity", "weird_action", {})
    assert exc_info.value.entity_type == "unknown_entity"
    assert exc_info.value.action == "weird_action"


def test_parse_payload_handles_null_payload():
    """An Event row may have an empty payload dict; parsing should still work
    for event types whose required-fields list is empty (e.g. TaskReady)."""
    parsed = parse_payload("task", "ready", None)
    assert isinstance(parsed, TaskReady)


# ── Registry coverage ────────────────────────────────────────────────────────


def test_registry_keys_match_entity_action_pairs():
    """Every class in the registry has its (ENTITY_TYPE, ACTION) used as
    the key — no drift between class metadata and registry indexing."""
    for (entity_type, action), cls in EVENT_REGISTRY.items():
        assert cls.ENTITY_TYPE == entity_type
        assert cls.ACTION == action


def test_registry_has_no_duplicate_keys():
    """If two classes claimed the same (entity_type, action), only one
    would appear in the registry — verify uniqueness explicitly."""
    from treadmill_api.events.registry import _REGISTRY_CLASSES

    seen: set[tuple[str, str]] = set()
    for cls in _REGISTRY_CLASSES:
        key = (cls.ENTITY_TYPE, cls.ACTION)
        assert key not in seen, f"duplicate registry key: {key}"
        seen.add(key)


def test_registry_covers_phase_2_minimum_events():
    """Phase 2 ships with at least these event types. New ones may be
    added; this set may not shrink."""
    expected = {
        ("task", "registered"),
        ("task", "ready"),
        ("task", "cancelled"),
        ("step", "ready"),
        ("step", "started"),
        ("step", "completed"),
        ("step", "failed"),
        ("step", "cancelled"),
        ("github", "pr_opened"),
        ("github", "pr_synchronize"),
        ("github", "pr_merged"),
        ("github", "pr_review_submitted"),
        ("github", "check_run_completed"),
        ("plan", "registered"),
        ("plan", "activated"),
        ("plan", "completed"),
    }
    missing = expected - set(EVENT_REGISTRY.keys())
    assert not missing, f"registry missing expected event types: {missing}"


# ── StepOutput envelope (ADR-0012) ────────────────────────────────────────────


def test_step_output_envelope_round_trip():
    """ADR-0012's envelope round-trips cleanly through Pydantic's JSON
    mode — the same shape lands at the consumer that the worker emits."""
    original = StepOutput(
        summary="Add /health endpoint",
        decision="pushed",
        commit_sha="deadbeef" * 5,
        artifacts=[
            Artifact(kind="branch", value="task/abc-feat"),
            Artifact(
                kind="pr_url",
                value="https://github.com/x/y/pull/42",
                label="open the PR",
            ),
        ],
        payload={"pr_number": 42},
        metadata=Metadata(model="claude-opus-4-7", input_tokens=1000),
    )
    encoded = original.model_dump(mode="json")
    decoded = StepOutput.model_validate(encoded)
    assert decoded == original


def test_step_output_required_fields_only():
    """``summary`` and ``decision`` are required; everything else has a
    sensible default. The minimum-viable envelope still validates."""
    output = StepOutput(summary="all good", decision="pushed")
    assert output.commit_sha is None
    assert output.artifacts == []
    assert output.payload == {}
    assert output.metadata == Metadata()


def test_step_output_forbids_extra_top_level_keys():
    """``extra="forbid"`` rejects unknown top-level fields — the discipline
    that catches malformed envelopes at the boundary. Per-workflow extras
    must go in ``payload``, not at the top level."""
    with pytest.raises(ValidationError):
        StepOutput.model_validate(
            {
                "summary": "x",
                "decision": "pushed",
                "pr_number": 42,  # top-level extras forbidden — must live in payload
            }
        )


def test_step_output_missing_required_field_raises():
    """``summary`` and ``decision`` are required — omitting either fails."""
    with pytest.raises(ValidationError):
        StepOutput.model_validate({"decision": "pushed"})  # missing summary
    with pytest.raises(ValidationError):
        StepOutput.model_validate({"summary": "x"})  # missing decision


def test_artifact_kind_is_strict_literal():
    """``Artifact.kind`` is a ``Literal[...]`` over the seven supported kinds.
    Adding a kind requires a code + ADR change; arbitrary strings fail."""
    Artifact(kind="branch", value="task/x")  # valid
    Artifact(kind="pr_url", value="https://x")  # valid
    with pytest.raises(ValidationError):
        Artifact.model_validate({"kind": "not-a-real-kind", "value": "x"})


def test_artifact_forbids_extra_keys():
    with pytest.raises(ValidationError):
        Artifact.model_validate(
            {"kind": "branch", "value": "task/x", "unexpected": "field"},
        )


def test_metadata_all_optional_and_forbids_extras():
    """``Metadata`` defaults to all-None (not every step has tokens etc.)
    but still forbids unknown top-level keys — those belong in ``extra``."""
    md = Metadata()
    assert md.model == md.input_tokens == md.output_tokens is None
    assert md.cost_usd is None and md.duration_ms is None
    assert md.extra == {}
    # ``extra`` is the operator-escape hatch.
    md2 = Metadata(extra={"session_id": "abc", "retries": 1})
    assert md2.extra["session_id"] == "abc"
    # But top-level unknowns still raise.
    with pytest.raises(ValidationError):
        Metadata.model_validate({"session_id": "abc"})


def test_step_completed_round_trip_with_full_envelope():
    """Worker-shape ``wf-author`` envelope round-trips through the consumer
    via the registry exactly as ADR-0012 specifies the convention map."""
    original = StepCompleted(
        completed_at=datetime.now(timezone.utc),
        output=StepOutput(
            summary="branch pushed",
            decision="pushed",
            commit_sha="deadbeef" * 5,
            artifacts=[
                Artifact(kind="branch", value="task/abc-feat"),
                Artifact(kind="pr_url", value="https://github.com/x/y/pull/42"),
            ],
            payload={"pr_number": 42},
        ),
    )
    parsed = _round_trip(original)
    assert isinstance(parsed, StepCompleted)
    assert isinstance(parsed.output, StepOutput)
    assert parsed.output.commit_sha == "deadbeef" * 5
    assert parsed.output.payload["pr_number"] == 42
    # The branch artifact survives in artifacts[].
    branches = [a.value for a in parsed.output.artifacts if a.kind == "branch"]
    assert branches == ["task/abc-feat"]


def test_step_completed_rejects_dict_output_missing_required_fields():
    """``StepCompleted.output`` is now strict ``StepOutput``: a dict missing
    ``summary`` / ``decision`` fails the parse_payload gate. This is the
    contract change from the Week-2-closure union — there is no longer
    a raw-dict fallback at the wire boundary; the envelope is the only
    accepted shape."""
    with pytest.raises(ValidationError):
        StepCompleted.model_validate(
            {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "output": {"branch": "task/x", "pr_number": 1},
            }
        )


def test_step_completed_rejects_unknown_decision_keys_in_payload_top_level():
    """Top-level ``decision`` is a free string (ADR-0012), but unknown
    *top-level* envelope keys still fail. The discipline forces convention
    fields (``pr_number``, ``task_directive``, etc.) into ``payload``."""
    with pytest.raises(ValidationError):
        StepCompleted.model_validate(
            {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "output": {
                    "summary": "x",
                    "decision": "pushed",
                    "pr_number": 42,  # not allowed at top-level
                },
            }
        )
