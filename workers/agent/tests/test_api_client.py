"""Decoder + transport tests for the worker's API client.

The decoder is the seam between the JSON shape returned by GET
/api/v1/steps/{id} and the worker's typed ``WorkerContext``. We test it
directly so a schema drift on the API side surfaces here, not as a
runtime ``KeyError`` in the runner.
"""

from __future__ import annotations

import uuid

import pytest
from pytest_httpx import HTTPXMock

from treadmill_agent.api_client import ApiClient, _decode_context


def _sample_response(
    *,
    with_skill: bool = True,
    with_hook: bool = True,
    prior_steps: list[dict] | None = None,
) -> dict:
    base = {
        "step": {
            "id": "11111111-0000-0000-0000-000000000001",
            "run_id": "22222222-0000-0000-0000-000000000002",
            "step_index": 0,
            "step_name": "author",
            "role_id": "role-author",
            "status": "pending",
        },
        "run": {
            "id": "22222222-0000-0000-0000-000000000002",
            "task_id": "33333333-0000-0000-0000-000000000003",
            "workflow_version_id": "44444444-0000-0000-0000-000000000004",
            "workflow_id": "wf-author",
            "workflow_version": 1,
            "trigger": "registered",
        },
        "task": {
            "id": "33333333-0000-0000-0000-000000000003",
            "plan_id": "55555555-0000-0000-0000-000000000005",
            "repo": "owner/repo",
            "title": "Add a thing",
            "description": "longer description",
        },
        "plan": {
            "id": "55555555-0000-0000-0000-000000000005",
            "repo": "owner/repo",
            "intent": "the goal",
            "doc_path": "docs/plans/x.md",
        },
        "role": {
            "id": "role-author",
            "model": "claude-opus-4-7",
            "system_prompt": "be a coder",
            "output_kind": "code",
            "skills": (
                [{"id": "skill-author", "name": "authoring", "content": "do good"}]
                if with_skill else []
            ),
            "hooks": (
                [{
                    "id": "hook-pre", "name": "pre",
                    "event": "PreToolUse", "matcher": None, "command": "echo",
                }]
                if with_hook else []
            ),
        },
        "pr_number": None,
    }
    if prior_steps is not None:
        base["prior_steps"] = prior_steps
    return base


def test_decoder_maps_top_level_fields() -> None:
    ctx = _decode_context(_sample_response())
    assert ctx.step_id == "11111111-0000-0000-0000-000000000001"
    assert ctx.task_id == "33333333-0000-0000-0000-000000000003"
    assert ctx.plan_id == "55555555-0000-0000-0000-000000000005"
    assert ctx.run_id == "22222222-0000-0000-0000-000000000002"
    assert ctx.repo == "owner/repo"
    assert ctx.title == "Add a thing"
    assert ctx.description == "longer description"
    assert ctx.workflow_id == "wf-author"
    assert ctx.workflow_version == 1
    assert ctx.plan_intent == "the goal"


def test_decoder_role_includes_resolved_skills_and_hooks() -> None:
    ctx = _decode_context(_sample_response())
    assert ctx.role.id == "role-author"
    assert ctx.role.model == "claude-opus-4-7"
    assert ctx.role.system_prompt == "be a coder"
    assert len(ctx.role.skills) == 1
    assert ctx.role.skills[0].content == "do good"
    assert len(ctx.role.hooks) == 1
    assert ctx.role.hooks[0].command == "echo"


def test_decoder_handles_empty_skills_and_hooks() -> None:
    ctx = _decode_context(_sample_response(with_skill=False, with_hook=False))
    assert ctx.role.skills == []
    assert ctx.role.hooks == []


def test_decoder_defaults_prior_steps_to_empty_when_absent() -> None:
    """A response without a ``prior_steps`` key (e.g. a single-step
    workflow's first step, or an older mock from before A.4) decodes to
    an empty list — forward-compat by construction."""
    body = _sample_response()
    assert "prior_steps" not in body
    ctx = _decode_context(body)
    assert ctx.prior_steps == []


def test_decoder_maps_prior_steps() -> None:
    """The decoder lifts each prior-step entry through into a typed
    ``PriorStep``. The ``output`` field stays a raw dict because the
    envelope (ADR-0012) is consumed conventionally on the worker side
    rather than statically typed at the boundary (see PriorStep doc)."""
    prior = [
        {
            "step_index": 0,
            "step_name": "analyze",
            "role_id": "role-feedback-analyzer",
            "status": "completed",
            "output": {
                "summary": "classified comments into a task directive",
                "decision": "plan-ready",
                "commit_sha": "abc123",
                "artifacts": [],
                "payload": {
                    "task_directive": {
                        "summary": "Fix the nullable bug",
                        "files": ["foo.py"],
                        "intent": "Guard against None inputs.",
                    },
                },
                "metadata": {},
            },
        },
        {
            "step_index": 1,
            "step_name": "act",
            "role_id": "role-code-author",
            "status": "completed",
            "output": None,
        },
    ]
    ctx = _decode_context(_sample_response(prior_steps=prior))
    assert len(ctx.prior_steps) == 2

    first = ctx.prior_steps[0]
    assert first.step_index == 0
    assert first.step_name == "analyze"
    assert first.role_id == "role-feedback-analyzer"
    assert first.status == "completed"
    assert first.output is not None
    # The decoder passes the envelope through as a raw dict — the worker
    # reads ``payload.task_directive`` conventionally per ADR-0012/0015.
    assert first.output["decision"] == "plan-ready"
    assert (
        first.output["payload"]["task_directive"]["files"] == ["foo.py"]
    )

    second = ctx.prior_steps[1]
    assert second.step_index == 1
    assert second.output is None


def test_fetch_step_context_calls_get(httpx_mock: HTTPXMock) -> None:
    body = _sample_response()
    step_id = body["step"]["id"]
    httpx_mock.add_response(
        method="GET",
        url=f"http://fake-api/api/v1/steps/{step_id}",
        json=body,
    )
    with ApiClient("http://fake-api") as client:
        ctx = client.fetch_step_context(step_id)
    assert ctx.step_id == step_id


def test_fetch_step_context_raises_on_404(httpx_mock: HTTPXMock) -> None:
    step_id = "11111111-0000-0000-0000-000000000001"
    httpx_mock.add_response(
        method="GET",
        url=f"http://fake-api/api/v1/steps/{step_id}",
        status_code=404, json={"detail": "step not found"},
    )
    with ApiClient("http://fake-api") as client:
        with pytest.raises(Exception):
            client.fetch_step_context(step_id)
