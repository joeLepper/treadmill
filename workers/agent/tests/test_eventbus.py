"""Event publisher tests.

The publisher is one tiny seam — record-shape correctness and SNS
attribute wiring — so unit tests with a fake SNS client cover its
contract end-to-end without needing moto. Phase 2 (A.1) added Pydantic
validation at publish; the additional tests at the bottom of this file
lock in that the typed payload classes are exercised before SNS sees
the body.
"""

from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError

from treadmill_agent.eventbus import EventPublisher


class _FakeSns:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def publish(self, **kwargs) -> None:
        self.calls.append(kwargs)


def _publisher() -> tuple[EventPublisher, _FakeSns]:
    fake = _FakeSns()
    return EventPublisher(fake, "arn:aws:sns:us-east-1:1:treadmill-events"), fake


def _ids() -> dict:
    return dict(
        task_id=str(uuid.uuid4()),
        plan_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        step_id=str(uuid.uuid4()),
    )


def test_step_started_publishes_with_attributes() -> None:
    pub, fake = _publisher()
    ids = _ids()
    pub.publish_step_started(**ids)
    assert len(fake.calls) == 1
    body = json.loads(fake.calls[0]["Message"])
    assert body["entity_type"] == "step"
    assert body["action"] == "started"
    assert body["task_id"] == ids["task_id"]
    assert "started_at" in body["payload"]
    assert uuid.UUID(body["event_id"])  # well-formed
    attrs = fake.calls[0]["MessageAttributes"]
    assert attrs["entity_type"]["StringValue"] == "step"
    assert attrs["action"]["StringValue"] == "started"


def test_step_completed_carries_output() -> None:
    """Per ADR-0012, the publisher accepts a dict that matches the
    ``StepOutput`` envelope shape; the inflated wire body carries every
    top-level envelope field with the documented defaults."""
    pub, fake = _publisher()
    ids = _ids()
    output = {
        "summary": "done",
        "decision": "pushed",
        "commit_sha": "deadbeef",
        "artifacts": [
            {"kind": "branch", "value": "task/x"},
        ],
        "payload": {"pr_number": 42},
    }
    pub.publish_step_completed(**ids, output=output)
    body = json.loads(fake.calls[0]["Message"])
    assert body["action"] == "completed"
    out = body["payload"]["output"]
    assert out["summary"] == "done"
    assert out["decision"] == "pushed"
    assert out["commit_sha"] == "deadbeef"
    assert out["artifacts"] == [
        {"kind": "branch", "value": "task/x", "label": None},
    ]
    assert out["payload"] == {"pr_number": 42}


def test_step_failed_carries_error() -> None:
    pub, fake = _publisher()
    ids = _ids()
    pub.publish_step_failed(**ids, error="exit 1: boom")
    body = json.loads(fake.calls[0]["Message"])
    assert body["action"] == "failed"
    assert body["payload"]["error"] == "exit 1: boom"


def test_unwired_publisher_drops_silently() -> None:
    """When neither sns_client nor topic_arn is set, publish is a no-op
    that logs at INFO. Useful for tests / dry-runs."""
    pub = EventPublisher(sns_client=None, topic_arn=None)
    pub.publish_step_started(**_ids())  # should not raise


def test_each_event_gets_unique_id() -> None:
    pub, fake = _publisher()
    ids = _ids()
    pub.publish_step_started(**ids)
    pub.publish_step_completed(
        **ids,
        output={
            "summary": "done",
            "decision": "pushed",
            "artifacts": [{"kind": "branch", "value": "task/x"}],
            "payload": {"pr_number": 1},
        },
    )
    body0 = json.loads(fake.calls[0]["Message"])
    body1 = json.loads(fake.calls[1]["Message"])
    assert body0["event_id"] != body1["event_id"]


# ── Pydantic validation at publish (A.1) ──────────────────────────────────────


def test_step_completed_payload_validates_against_typed_model() -> None:
    """Round-trip the wire body through the API's registry parse_payload to
    prove the worker is producing a body the consumer can parse without
    drift. Per ADR-0012, the wire shape is the ``StepOutput`` envelope."""
    from treadmill_api.events.registry import parse_payload
    from treadmill_api.events.step import StepCompleted
    from treadmill_api.events.step_output import StepOutput

    pub, fake = _publisher()
    ids = _ids()
    output = {
        "summary": "did the thing",
        "decision": "pushed",
        "commit_sha": "deadbeef",
        "artifacts": [
            {"kind": "branch", "value": "task/x"},
            {"kind": "pr_url", "value": "https://example.com/x/pull/42"},
        ],
        "payload": {"pr_number": 42},
    }
    pub.publish_step_completed(**ids, output=output)
    body = json.loads(fake.calls[0]["Message"])
    typed = parse_payload(body["entity_type"], body["action"], body["payload"])
    assert isinstance(typed, StepCompleted)
    assert isinstance(typed.output, StepOutput)
    assert typed.output.summary == "did the thing"
    assert typed.output.decision == "pushed"
    assert typed.output.commit_sha == "deadbeef"
    assert typed.output.payload["pr_number"] == 42
    branches = [a.value for a in typed.output.artifacts if a.kind == "branch"]
    assert branches == ["task/x"]
    pr_urls = [a.value for a in typed.output.artifacts if a.kind == "pr_url"]
    assert pr_urls == ["https://example.com/x/pull/42"]


def test_publish_rejects_invalid_output_dict() -> None:
    """If the worker hands a malformed dict (missing required envelope
    field) to publish_step_completed, the Pydantic ValidationError
    propagates — the worker bug surfaces at publish, not later in the
    consumer."""
    pub, _fake = _publisher()
    ids = _ids()
    # Missing required ``decision`` — fails ``StepOutput.model_validate``.
    with pytest.raises(ValidationError):
        pub.publish_step_completed(**ids, output={"summary": "x"})


def test_publish_rejects_unknown_top_level_envelope_key() -> None:
    """Top-level keys outside the envelope's known fields fail per
    ADR-0012's ``extra="forbid"`` discipline. ``pr_number`` is convention
    in ``payload``, not at top-level."""
    pub, _fake = _publisher()
    ids = _ids()
    with pytest.raises(ValidationError):
        pub.publish_step_completed(
            **ids,
            output={"summary": "x", "decision": "pushed", "pr_number": 42},
        )


def test_publish_accepts_step_output_directly() -> None:
    """Worker callsites may build a ``StepOutput`` themselves instead of
    leaning on dict coercion. The publisher dumps it to wire JSON."""
    from treadmill_agent.events import Artifact, StepOutput

    pub, fake = _publisher()
    ids = _ids()
    typed_output = StepOutput(
        summary="did it",
        decision="pushed",
        commit_sha="cafef00d",
        artifacts=[Artifact(kind="branch", value="task/y")],
        payload={"pr_number": 7},
    )
    pub.publish_step_completed(**ids, output=typed_output)
    body = json.loads(fake.calls[0]["Message"])
    out = body["payload"]["output"]
    assert out["summary"] == "did it"
    assert out["decision"] == "pushed"
    assert out["commit_sha"] == "cafef00d"
    assert out["artifacts"] == [
        {"kind": "branch", "value": "task/y", "label": None},
    ]
    assert out["payload"] == {"pr_number": 7}


def test_publish_validates_dict_into_step_output_at_publish() -> None:
    """A bare dict is validated through ``StepOutput.model_validate`` at
    publish time so producer bugs surface at the worker, not after the
    message has crossed SNS. The resulting wire body carries the
    envelope's normalized shape (artifact ``label`` defaults to ``None``
    after validation, etc.)."""
    pub, fake = _publisher()
    ids = _ids()
    pub.publish_step_completed(
        **ids,
        output={
            "summary": "done",
            "decision": "pushed",
            "commit_sha": "deadbeef",
            "artifacts": [{"kind": "branch", "value": "task/z"}],
        },
    )
    body = json.loads(fake.calls[0]["Message"])
    out = body["payload"]["output"]
    # Normalized via Pydantic: ``label`` filled in, ``payload`` defaults
    # to empty dict, ``metadata`` defaults to all-None.
    assert out["artifacts"][0]["label"] is None
    assert out["payload"] == {}
    assert out["metadata"]["model"] is None


# ── ADR-0020: token_usage on step.completed ──────────────────────────────────


def test_publish_step_completed_carries_token_usage_when_provided() -> None:
    """ADR-0020: when the runner passes ``token_usage`` to
    ``publish_step_completed``, the wire body's payload carries a
    top-level ``token_usage`` block with all four counters + the model.
    The block sits next to ``output`` — *not* nested inside
    ``output.metadata`` — because token usage is step-execution telemetry
    and the API projects it onto dedicated columns."""
    pub, fake = _publisher()
    ids = _ids()
    pub.publish_step_completed(
        **ids,
        output={
            "summary": "did it",
            "decision": "pushed",
            "artifacts": [{"kind": "branch", "value": "task/x"}],
        },
        token_usage={
            "input_tokens": 100,
            "output_tokens": 40,
            "cache_creation_tokens": 8,
            "cache_read_tokens": 16,
            "model": "claude-opus-4-7",
        },
    )
    body = json.loads(fake.calls[0]["Message"])
    usage = body["payload"]["token_usage"]
    assert usage == {
        "input_tokens": 100,
        "output_tokens": 40,
        "cache_creation_tokens": 8,
        "cache_read_tokens": 16,
        "model": "claude-opus-4-7",
    }
    # Sibling of ``output``, never folded into it.
    assert "token_usage" not in body["payload"]["output"]
    assert "token_usage" not in body["payload"]["output"].get("metadata", {})


def test_publish_step_completed_omits_token_usage_when_absent() -> None:
    """When the runner passes ``token_usage=None`` (dry-run, wf-validate,
    or any step that made no LLM call), the wire body still parses but
    ``payload['token_usage']`` is ``None`` — the consumer then writes
    NULLs to the five workflow_run_steps columns."""
    pub, fake = _publisher()
    ids = _ids()
    pub.publish_step_completed(
        **ids,
        output={
            "summary": "did it",
            "decision": "pushed",
            "artifacts": [{"kind": "branch", "value": "task/x"}],
        },
    )
    body = json.loads(fake.calls[0]["Message"])
    assert body["payload"]["token_usage"] is None


def test_publish_step_completed_round_trips_through_step_completed_parser() -> None:
    """The wire body carrying ``token_usage`` must round-trip through the
    API's typed ``StepCompleted`` parser — same drift contract as the
    rest of the envelope. A missing or differently-named sub-field would
    surface here."""
    from treadmill_api.events.registry import parse_payload
    from treadmill_api.events.step import StepCompleted, StepTokenUsage

    pub, fake = _publisher()
    ids = _ids()
    pub.publish_step_completed(
        **ids,
        output={
            "summary": "did the thing",
            "decision": "pushed",
            "artifacts": [{"kind": "branch", "value": "task/x"}],
        },
        token_usage={
            "input_tokens": 11,
            "output_tokens": 22,
            "cache_creation_tokens": 33,
            "cache_read_tokens": 44,
            "model": "claude-haiku-4-5-20251001",
        },
    )
    body = json.loads(fake.calls[0]["Message"])
    typed = parse_payload(body["entity_type"], body["action"], body["payload"])
    assert isinstance(typed, StepCompleted)
    assert isinstance(typed.token_usage, StepTokenUsage)
    assert typed.token_usage.input_tokens == 11
    assert typed.token_usage.output_tokens == 22
    assert typed.token_usage.cache_creation_tokens == 33
    assert typed.token_usage.cache_read_tokens == 44
    assert typed.token_usage.model == "claude-haiku-4-5-20251001"


def test_publish_rejects_malformed_token_usage_dict() -> None:
    """A malformed ``token_usage`` dict (missing required sub-field) fails
    Pydantic validation at publish — the worker bug surfaces here, not
    on the consumer."""
    pub, _fake = _publisher()
    ids = _ids()
    # Missing ``model`` — every StepTokenUsage sub-field is required.
    with pytest.raises(ValidationError):
        pub.publish_step_completed(
            **ids,
            output={"summary": "x", "decision": "pushed"},
            token_usage={
                "input_tokens": 1,
                "output_tokens": 2,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 0,
            },
        )


def test_publish_step_started_serializes_iso_timestamp() -> None:
    """Wire format is JSON; the typed StepStarted.started_at must come
    back as an ISO-8601 string after model_dump(mode='json')."""
    pub, fake = _publisher()
    ids = _ids()
    pub.publish_step_started(**ids)
    body = json.loads(fake.calls[0]["Message"])
    assert isinstance(body["payload"]["started_at"], str)
    # parses back to a datetime cleanly — the consumer relies on this.
    from datetime import datetime
    parsed = datetime.fromisoformat(body["payload"]["started_at"])
    assert parsed.tzinfo is not None  # always UTC-aware
