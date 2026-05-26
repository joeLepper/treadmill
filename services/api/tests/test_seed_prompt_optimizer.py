"""Unit tests for the ADR-0053 Wave 2 seeding shape.

Asserts that ``seed_starters_if_empty`` writes the new
``role-prompt-optimizer`` Role + ``wf-tune-judge-prompts`` Workflow +
exactly one ``WorkflowVersion`` for that workflow when run against a
fresh DB.

Mirrors ``test_seed_schedules.py``'s fixture pattern — a ``MagicMock``
session whose ``execute().scalar_one()`` returns the desired
``existing_count``. The seed function calls ``session.add`` for each
row it would insert; we inspect the call list to verify the new
ADR-0053 Wave 2 rows are present without needing a live Postgres.

Validation per the ADR-0053 plan:
  ``cd services/api && uv run pytest tests/test_seed_prompt_optimizer.py -q``
"""

from __future__ import annotations

from unittest.mock import MagicMock

from treadmill_api.models import (
    Role,
    Workflow,
    WorkflowVersion,
    WorkflowVersionStep,
)
from treadmill_api.starters import seed_starters_if_empty


def _make_session(existing_role_count: int = 0) -> MagicMock:
    """Mock session whose execute().scalar_one() returns
    ``existing_role_count`` (drives the empty-check inside
    ``seed_starters_if_empty``)."""
    session = MagicMock()
    session.execute.return_value.scalar_one.return_value = existing_role_count
    return session


def _added_of(session: MagicMock, model_cls: type) -> list:
    """Return the instances of ``model_cls`` passed to ``session.add``."""
    return [
        c.args[0]
        for c in session.add.call_args_list
        if isinstance(c.args[0], model_cls)
    ]


# ── Role row ─────────────────────────────────────────────────────────────────


def test_seed_adds_role_prompt_optimizer() -> None:
    session = _make_session()
    seed_starters_if_empty(session)

    role_ids = {r.id for r in _added_of(session, Role)}
    assert "role-prompt-optimizer" in role_ids, (
        "seed_starters_if_empty must insert a Role row with "
        "id='role-prompt-optimizer' on a fresh DB (ADR-0053 Wave 2)"
    )


def test_role_prompt_optimizer_prompt_contains_marker() -> None:
    """The role's prompt must contain the literal marker string
    ``role-prompt-optimizer`` so the test catches accidental wholesale
    rewrites of the operator-authored prompt."""
    session = _make_session()
    seed_starters_if_empty(session)

    role = next(
        r for r in _added_of(session, Role)
        if r.id == "role-prompt-optimizer"
    )
    assert "role-prompt-optimizer" in role.system_prompt, (
        "role-prompt-optimizer's prompt must contain the literal "
        "marker string 'role-prompt-optimizer'"
    )


# ── Workflow row ─────────────────────────────────────────────────────────────


def test_seed_adds_wf_tune_judge_prompts() -> None:
    session = _make_session()
    seed_starters_if_empty(session)

    wf_ids = {w.id for w in _added_of(session, Workflow)}
    assert "wf-tune-judge-prompts" in wf_ids, (
        "seed_starters_if_empty must insert a Workflow row with "
        "id='wf-tune-judge-prompts' on a fresh DB (ADR-0053 Wave 2)"
    )


# ── WorkflowVersion row (the test the task names explicitly) ─────────────────


def test_exactly_one_workflow_version_for_wf_tune_judge_prompts() -> None:
    """The seed must register exactly one WorkflowVersion (version=1)
    for ``wf-tune-judge-prompts``. A drift here means later operator
    triggers wouldn't resolve a runnable version."""
    session = _make_session()
    seed_starters_if_empty(session)

    versions = [
        v for v in _added_of(session, WorkflowVersion)
        if v.workflow_id == "wf-tune-judge-prompts"
    ]
    assert len(versions) == 1, (
        f"expected exactly one WorkflowVersion for "
        f"'wf-tune-judge-prompts', got {len(versions)}"
    )
    assert versions[0].version == 1, (
        f"expected version=1, got {versions[0].version!r}"
    )


# ── WorkflowVersionStep wiring (sanity — single 'optimize' step) ─────────────


def test_wf_tune_judge_prompts_has_single_optimize_step() -> None:
    """The workflow has a single step named ``optimize`` bound to
    ``role-prompt-optimizer`` per ADR-0053. Verifying via the
    ``STARTERS`` constant rather than the session.add calls because
    ``WorkflowVersionStep.workflow_version_id`` is server-generated on a
    real DB; the constant is the canonical source of truth."""
    from treadmill_api.starters import STARTERS

    wf = next(w for w in STARTERS if w["id"] == "wf-tune-judge-prompts")
    assert len(wf["steps"]) == 1
    assert wf["steps"][0]["name"] == "optimize"
    assert wf["steps"][0]["role_id"] == "role-prompt-optimizer"


def test_wf_tune_judge_prompts_step_row_added() -> None:
    """A WorkflowVersionStep row for the ``optimize`` step is added
    during seeding. (The ``workflow_version_id`` FK is not asserted —
    it's server-generated on a real DB and ``None`` under MagicMock.)"""
    session = _make_session()
    seed_starters_if_empty(session)

    steps = [
        s for s in _added_of(session, WorkflowVersionStep)
        if s.role_id == "role-prompt-optimizer"
    ]
    assert len(steps) == 1, (
        f"expected exactly one WorkflowVersionStep bound to "
        f"role-prompt-optimizer, got {len(steps)}"
    )
    assert steps[0].step_name == "optimize"
    assert steps[0].step_index == 0


# ── Idempotency (non-empty DB short-circuits before adding) ──────────────────


def test_seed_skips_when_roles_already_exist() -> None:
    """When the empty-check sees an existing role, ``session.add`` is
    never called — the new ADR-0053 rows are not duplicated."""
    session = _make_session(existing_role_count=1)
    result = seed_starters_if_empty(session)

    assert result == 0
    session.add.assert_not_called()
    session.commit.assert_not_called()
