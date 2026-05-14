"""Steps router — worker-facing.

A worker that has just received a ``step.ready`` claim from the work
queue calls ``GET /api/v1/steps/{step_id}`` to fetch everything it needs
to do the work in a single round-trip:

  * the step (run reference, step_index, step_name, status)
  * the parent run (task reference, workflow_version reference)
  * the parent task (repo, title, description, plan reference)
  * the parent plan (intent + doc_path so the worker can include plan
    context in its prompt)
  * the role (model, system_prompt, ordered skills + hooks)
  * the resolved skill + hook content (so the worker doesn't need to chase
    additional endpoints)

Bunkhouse uses this same "compute the worker context server-side" shape;
the worker stays simple and the server holds the join logic.

This router is read-only at v0. Workers do not mutate state through HTTP
— per ADR-0011 + the user's "fully event-driven" rule, worker writes are
events on the SNS bus, applied to state by the coordination consumer.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.dependencies_db import get_session
from treadmill_api.models import (
    Hook,
    OutputKind,
    Plan,
    Role,
    RoleHook,
    RoleSkill,
    Skill,
    Task,
    TaskPR,
    TaskValidation,
    Workflow,
    WorkflowRun,
    WorkflowRunStep,
    WorkflowVersion,
)


router = APIRouter(prefix="/api/v1/steps", tags=["steps"])


# ── Response shapes — flat enough to ship to a Go/Rust worker later ──────────


class _StepBlock(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    step_index: int
    step_name: str
    role_id: str
    status: str


class _RunBlock(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    workflow_version_id: uuid.UUID
    workflow_id: str
    workflow_version: int
    trigger: str


class _TaskBlock(BaseModel):
    id: uuid.UUID
    plan_id: uuid.UUID
    repo: str
    title: str
    description: str | None


class _PlanBlock(BaseModel):
    id: uuid.UUID
    repo: str
    intent: str | None
    doc_path: str | None


class _SkillBlock(BaseModel):
    id: str
    name: str
    content: str


class _HookBlock(BaseModel):
    id: str
    name: str
    event: str
    matcher: str | None
    command: str


class _RoleBlock(BaseModel):
    id: str
    model: str
    system_prompt: str
    output_kind: OutputKind
    """Per ADR-0022 — the runner reads this to pick its per-kind
    disposition handler (code / review / analysis / plan_doc)."""
    skills: list[_SkillBlock]
    hooks: list[_HookBlock]


class _ValidationBlock(BaseModel):
    id: uuid.UUID
    kind: str
    description: str
    script: str | None
    prompt: str | None


class PriorStepBlock(BaseModel):
    """A completed prior step in the same run, returned as part of the
    worker context per ADR-0015.

    ``output`` is intentionally typed as ``dict | None`` rather than the
    ``StepOutput`` envelope class from ADR-0012. The reason: A.2 (the
    parallel agent) is rewriting ``StepCompleted.output`` from the
    Week-2-closure union into ``StepOutput`` in lockstep with this
    change. Until A.2 lands the column carries the union shape; after
    A.2 lands it conforms to the envelope. **Either way, returning the
    raw JSON dict is correct here** — this router is a pass-through for
    whatever the worker wrote, and the worker's own decoder (and the
    consumer that wrote it) is what types the value at the boundary.
    Forward-compat by construction.
    """

    step_index: int
    step_name: str
    role_id: str
    status: str
    output: dict | None


class WorkerContextResponse(BaseModel):
    """The full bundle a worker needs to execute one step."""

    step: _StepBlock
    run: _RunBlock
    task: _TaskBlock
    plan: _PlanBlock
    role: _RoleBlock
    pr_number: int | None = None
    """Per ADR-0022 — the PR number this step relates to. Derived from
    ``task_prs`` when a row exists for the task; ``None`` otherwise.
    Required for review-kind steps (the worker raises
    ``MissingContextError`` when absent); optional for other kinds.
    The per-kind handler enforces."""
    prior_steps: list[PriorStepBlock] = []
    """Completed prior steps in the same run, ordered by ``step_index``.

    Empty for the first step of a run and for any single-step workflow.
    The worker's prompt-composer folds these into the role's input for
    multi-step workflows per ADR-0015 — e.g. ``wf-feedback`` step 2
    reads step 1's analyzer task_directive from ``prior_steps[0].output
    .payload.task_directive``. Pending / running / failed prior steps
    are intentionally **excluded** — the worker only consumes
    successfully-completed analyzer output.
    """
    task_validations: list[_ValidationBlock] = []
    """Task-specific validation checks defined in the plan-doc task spec.
    Per the 2026-05-14 learning, the code disposition runs these scripts
    before pushing to gate on self-validation."""


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.get("/{step_id}", response_model=WorkerContextResponse)
async def get_step_context(
    step_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WorkerContextResponse:
    """Return everything a worker needs to execute this step."""
    step = await session.get(WorkflowRunStep, step_id)
    if step is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="step not found",
        )

    # FK constraints guarantee these rows exist; if a lookup returns None
    # the database is in a corrupted state — surface it as a 500 with a
    # message that names the missing referent so on-call can act.
    run = await session.get(WorkflowRun, step.run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"workflow_run {step.run_id} referenced by step does not exist",
        )

    wv = await session.get(WorkflowVersion, run.workflow_version_id)
    if wv is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"workflow_version {run.workflow_version_id} referenced by run "
                "does not exist"
            ),
        )
    workflow = await session.get(Workflow, wv.workflow_id)
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"workflow {wv.workflow_id!r} referenced by workflow_version "
                "does not exist"
            ),
        )

    task = await session.get(Task, run.task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"task {run.task_id} referenced by run does not exist",
        )

    plan = await session.get(Plan, task.plan_id)
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"plan {task.plan_id} referenced by task does not exist",
        )

    role = await session.get(Role, step.role_id)
    if role is None:
        # role_id has ondelete=RESTRICT but FK is to roles.id; if the
        # role was somehow removed, this is data corruption — surface it.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"role {step.role_id!r} referenced by step does not exist",
        )

    # Skills + hooks for the role, ordered.
    skill_rows = (
        await session.execute(
            select(Skill)
            .join(RoleSkill, RoleSkill.skill_id == Skill.id)
            .where(RoleSkill.role_id == role.id)
            .order_by(RoleSkill.position)
        )
    ).scalars().all()
    hook_rows = (
        await session.execute(
            select(Hook)
            .join(RoleHook, RoleHook.hook_id == Hook.id)
            .where(RoleHook.role_id == role.id)
            .order_by(RoleHook.position)
        )
    ).scalars().all()

    # Per ADR-0022 — look up the task's PR number via the ``task_prs``
    # bridge if one exists. The bridge row is created by the worker when
    # it opens the PR (or by the webhook ingestion path for externally-
    # initiated PRs); a task that hasn't opened a PR yet has no row, so
    # ``pr_number`` stays ``None``. Per-kind handlers enforce per their
    # contract (review needs it; analysis / code / plan_doc don't).
    pr_row = (
        await session.execute(
            select(TaskPR.pr_number)
            .where(TaskPR.task_id == task.id)
            .order_by(TaskPR.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    # Prior steps for ADR-0015's multi-step workflows. Completed-only:
    # the worker only consumes finished analyzer output (pending /
    # running steps haven't produced anything to consume; failed steps
    # don't carry a usable directive). Ordered by ``step_index`` so the
    # worker can pick by position (``prior_steps[0]`` is the immediate
    # predecessor's analyzer in the two-step shape).
    prior_step_rows = (
        await session.execute(
            select(WorkflowRunStep)
            .where(
                WorkflowRunStep.run_id == step.run_id,
                WorkflowRunStep.step_index < step.step_index,
                WorkflowRunStep.status == "completed",
            )
            .order_by(WorkflowRunStep.step_index)
        )
    ).scalars().all()

    # Task-specific validation checks, ordered by position.
    # Per the 2026-05-14 learning, the code disposition runs these
    # scripts before pushing to gate on author-side self-validation.
    validation_rows = (
        await session.execute(
            select(TaskValidation)
            .where(TaskValidation.task_id == task.id)
            .order_by(TaskValidation.position)
        )
    ).scalars().all()

    return WorkerContextResponse(
        step=_StepBlock(
            id=step.id, run_id=step.run_id, step_index=step.step_index,
            step_name=step.step_name, role_id=step.role_id, status=step.status,
        ),
        run=_RunBlock(
            id=run.id, task_id=run.task_id,
            workflow_version_id=run.workflow_version_id,
            workflow_id=workflow.id, workflow_version=wv.version,
            trigger=run.trigger,
        ),
        task=_TaskBlock(
            id=task.id, plan_id=task.plan_id, repo=task.repo,
            title=task.title, description=task.description,
        ),
        plan=_PlanBlock(
            id=plan.id, repo=plan.repo, intent=plan.intent,
            doc_path=plan.doc_path,
        ),
        role=_RoleBlock(
            id=role.id, model=role.model, system_prompt=role.system_prompt,
            output_kind=role.output_kind,
            skills=[
                _SkillBlock(id=s.id, name=s.name, content=s.content)
                for s in skill_rows
            ],
            hooks=[
                _HookBlock(
                    id=h.id, name=h.name, event=h.event,
                    matcher=h.matcher, command=h.command,
                )
                for h in hook_rows
            ],
        ),
        pr_number=pr_row,
        prior_steps=[
            PriorStepBlock(
                step_index=ps.step_index,
                step_name=ps.step_name,
                role_id=ps.role_id,
                status=ps.status,
                output=ps.output,
            )
            for ps in prior_step_rows
        ],
        task_validations=[
            _ValidationBlock(
                id=v.id, kind=v.kind, description=v.description,
                script=v.script, prompt=v.prompt,
            )
            for v in validation_rows
        ],
    )
