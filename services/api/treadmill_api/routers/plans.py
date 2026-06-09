"""Plans router.

Per ADR-0010, Plans are first-class entities. The router supports two
intake paths:

  * Scenario 1 (pre-authored): ``POST /plans`` with ``doc_content`` → the
    server parses ``## sequence_of_work``, creates Plan(active) and one
    Task per spec.
  * Scenario 2 (intent only): ``POST /plans`` with ``intent`` → creates
    Plan(drafting); a future ``wf-plan`` workflow will produce the doc and
    activate the plan via ``POST /plans/{id}/submit-doc``.

Plan + task lifecycle events are emitted via the dispatcher's
``persist_and_publish`` helper. Scenario 1 fires both ``PlanRegistered``
and (in the same transaction, per decision #4) ``PlanActivated``.
Scenario 2 fires only ``PlanRegistered``; ``PlanActivated`` lands later
when ``submit-doc`` (or ``wf-plan``) attaches the doc.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.config import DeploymentMode, Settings, get_settings
from treadmill_api.dependencies_db import get_session
from treadmill_api.dispatch import Dispatcher, DispatchError, get_dispatcher
from treadmill_api.events.plan import (
    PlanActivated,
    PlanRegistered,
    PlanSubmitted,
)
from treadmill_api.events.system import SystemAutoSeededStarters
from treadmill_api.events.task import TaskRegistered
from treadmill_api.models import (
    Plan,
    Task,
    TaskDependency,
    TaskValidation,
    Workflow,
    WorkflowVersion,
)
from treadmill_api.parsers import (
    PlanDocFormatError,
    TaskSpec,
    parse_plan_doc,
    parse_plan_doc_frontmatter,
)
from treadmill_api.parsers.plan_doc import validate_unique_task_ids
from treadmill_api.team_config_store import TeamConfigStore

logger = logging.getLogger("treadmill.plans")


router = APIRouter(prefix="/api/v1/plans", tags=["plans"])


def get_team_config_store() -> TeamConfigStore:
    """FastAPI dependency factory for the :class:`TeamConfigStore`.

    Plain instantiation — same shape as ``get_dispatcher`` /
    ``get_settings``. The factory seam exists so tests can override
    the dependency via ``app.dependency_overrides`` to inject a fake
    store that returns ``None`` for every repo (the "no team_config
    row" code path) without touching the route signature.

    Task D of the combined ADR-0085+0086 plan added this as the first
    ``TeamConfigStore`` injection in ``plans.py``; subsequent routers
    that need it should re-export this same factory rather than
    constructing a duplicate.
    """
    return TeamConfigStore()


# ── Pydantic request / response shapes ────────────────────────────────────────


class PlanCreateRequest(BaseModel):
    """One of ``intent`` or ``doc_content`` must be present.

    * ``doc_content`` present → Scenario 1: server parses + spawns tasks.
    * ``intent`` present, ``doc_content`` absent → Scenario 2: drafting.

    ``dev`` is a fully-local-only fast-path flag (per D.10 in the
    2026-05-11 closure plan). When ``True`` AND
    ``TREADMILL_DEPLOYMENT_MODE=fully_local``, an intent-only submission
    short-circuits the ``wf-plan`` PR-merge gate: the plan is created
    active and an implicit single ``wf-author`` task is spawned with the
    intent as its description, dispatched immediately. Outside fully_local
    mode (dev_local, fully_remote) the flag is ignored with a logged
    warning so production traffic never accidentally side-steps planning.
    When ``doc_content`` is present, ``dev`` is a no-op — the standard
    doc path already produces an active plan.
    """

    repo: str = Field(..., min_length=1)
    intent: str | None = None
    doc_path: str | None = None
    doc_content: str | None = None
    created_by: str | None = None
    dev: bool = False

    @model_validator(mode="after")
    def require_intent_or_doc(self) -> "PlanCreateRequest":
        if self.intent is None and self.doc_content is None:
            raise ValueError(
                "either 'intent' (Scenario 2) or 'doc_content' (Scenario 1) is required"
            )
        return self


class PlanResponse(BaseModel):
    id: uuid.UUID
    repo: str
    intent: str | None
    doc_path: str | None
    parent_plan_id: uuid.UUID | None
    created_by: str | None
    created_at: datetime
    derived_status: str | None = None
    """Resolved plan state read from the ``plan_status`` VIEW.

    ``None`` when the plan row exists but the VIEW has not yet been read
    (e.g. on the immediate-after-create response when the plan was just
    INSERTed and the route did not LEFT JOIN). ``drafting`` is the default
    for a plan with no lifecycle events recorded yet.
    """


class TaskResponse(BaseModel):
    id: uuid.UUID
    plan_id: uuid.UUID
    repo: str
    title: str
    description: str | None
    workflow_version_id: uuid.UUID
    created_at: datetime
    derived_status: str | None = None


class PlansSubmitDocRequest(BaseModel):
    doc_path: str
    doc_content: str = Field(..., min_length=1)


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _resolve_workflow_version(session: AsyncSession, slug: str) -> uuid.UUID:
    """Find the latest WorkflowVersion id for a workflow slug, or raise 400."""
    workflow = await session.get(Workflow, slug)
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"workflow {slug!r} not registered; register it via the workflows router first",
        )
    result = await session.execute(
        select(WorkflowVersion)
        .where(WorkflowVersion.workflow_id == slug)
        .order_by(WorkflowVersion.version.desc())
        .limit(1)
    )
    version = result.scalar_one_or_none()
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"workflow {slug!r} has no versions yet",
        )
    return version.id


async def _spawn_tasks_from_specs(
    session: AsyncSession,
    dispatcher: Dispatcher,
    plan: Plan,
    specs: list[TaskSpec],
    created_by: str | None,
) -> list[Task]:
    """Translate the parsed TaskSpec list into Task rows, resolving each
    spec's workflow slug to a workflow_version_id.

    Side effects beyond ``tasks`` INSERTs:

      * Persist + publish a ``TaskRegistered`` event per task (A.6).
      * INSERT ``task_validations`` rows for each spec's ``validation:``
        list (D.3).
      * INSERT ``task_dependencies`` rows after sibling-id → UUID
        substitution + grammar validation (D.1).
    """
    # Pass 1: create task rows and remember the sibling-id → UUID map so
    # the dependency-expression rewriter in pass 2 can substitute. We can't
    # write task_dependencies in pass 1 because a sibling might not have a
    # UUID yet when its dependant is processed.
    tasks: list[Task] = []
    workflow_version_by_task: dict[uuid.UUID, uuid.UUID] = {}
    sibling_id_to_uuid: dict[str, uuid.UUID] = {}
    spec_by_task_id: dict[uuid.UUID, TaskSpec] = {}
    for spec in specs:
        wv_id = await _resolve_workflow_version(session, spec.workflow)
        task = Task(
            plan_id=plan.id,
            repo=plan.repo,
            title=spec.title,
            description=spec.intent,
            workflow_version_id=wv_id,
            created_by=created_by,
        )
        session.add(task)
        tasks.append(task)
        await session.flush()  # produces task.id
        workflow_version_by_task[task.id] = wv_id
        sibling_id_to_uuid[spec.id] = task.id
        spec_by_task_id[task.id] = spec

    # Pass 2: persist validations + dependencies, and emit lifecycle events.
    for task in tasks:
        spec = spec_by_task_id[task.id]
        # D.3 — task_validations. Parse now provides script/prompt.
        for index, check in enumerate(spec.validation):
            session.add(
                TaskValidation(
                    task_id=task.id,
                    position=index,
                    kind=check.kind,
                    description=check.description,
                    script=check.script,
                    prompt=check.prompt,
                )
            )
        # D.1 — task_dependencies (grammar-validate + substitute sibling UUIDs)
        for expr in spec.depends_on:
            substituted = _validate_and_substitute_dep_expr(
                expr, sibling_id_to_uuid, plan_task_id=spec.id,
            )
            session.add(
                TaskDependency(task_id=task.id, expression=substituted),
            )

    await session.flush()

    # A.6 — emit TaskRegistered per task. Must happen after flush so the
    # Event row carries the correct task_id reference.
    for task in tasks:
        await dispatcher.persist_and_publish(
            session,
            entity_type="task",
            action="registered",
            payload=TaskRegistered(
                repo=task.repo,
                title=task.title,
                workflow_version_id=workflow_version_by_task[task.id],
                plan_id=plan.id,
            ),
            plan_id=plan.id,
            task_id=task.id,
        )

    return tasks


async def _spawn_dev_wf_author_task(
    session: AsyncSession,
    dispatcher: Dispatcher,
    plan: Plan,
    intent: str,
    created_by: str | None,
) -> Task:
    """Spawn the implicit one-task wf-author run that the ``--dev`` flag
    creates for intent-only submissions in local mode (D.10).

    Mirrors a minimal subset of ``_spawn_tasks_from_specs``: resolve
    ``wf-author``'s latest version, INSERT the task, emit
    ``TaskRegistered``. The caller dispatches.
    """
    wv_id = await _resolve_workflow_version(session, "wf-author")
    title = (intent or "untitled").strip()[:200] or "untitled"
    task = Task(
        plan_id=plan.id,
        repo=plan.repo,
        title=title,
        description=intent or None,
        workflow_version_id=wv_id,
        created_by=created_by,
    )
    session.add(task)
    await session.flush()
    await dispatcher.persist_and_publish(
        session,
        entity_type="task",
        action="registered",
        payload=TaskRegistered(
            repo=task.repo,
            title=task.title,
            workflow_version_id=wv_id,
            plan_id=plan.id,
        ),
        plan_id=plan.id,
        task_id=task.id,
    )
    return task


# ── task_dependencies expression rewriter ─────────────────────────────────────

# Grammar accepted at v0:
#   task.<sibling-id>.pr_merged
#   task.<sibling-id>.run.completed
#   task.<sibling-id>.step.<NAME>.completed
#
# ``<sibling-id>`` references another task's plan-doc id (e.g. ``t0``) and
# is rewritten to its UUID before INSERT. Anything outside this grammar
# 400s — the rule engine downstream depends on the exact shape.
_DEP_RE = re.compile(
    r"^task\.(?P<sibling>[a-zA-Z0-9_-]+)"
    r"\.(?P<rest>pr_merged|run\.completed|step\.[a-zA-Z0-9_-]+\.completed)$"
)


def _validate_and_substitute_dep_expr(
    expression: str,
    sibling_uuid_map: dict[str, uuid.UUID],
    *,
    plan_task_id: str,
) -> str:
    """Validate a single ``depends_on`` expression and replace its sibling
    id with the resolved UUID. Raises ``HTTPException(400)`` on shape
    violations or unknown sibling references."""
    match = _DEP_RE.match(expression)
    if match is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"task {plan_task_id!r}: malformed depends_on expression "
                f"{expression!r}; expected one of "
                "task.<id>.pr_merged | task.<id>.run.completed | "
                "task.<id>.step.<name>.completed"
            ),
        )
    sibling_id = match.group("sibling")
    rest = match.group("rest")
    sibling_uuid = sibling_uuid_map.get(sibling_id)
    if sibling_uuid is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"task {plan_task_id!r}: depends_on references unknown "
                f"sibling id {sibling_id!r}; not found in this plan"
            ),
        )
    return f"task.{sibling_uuid}.{rest}"


def _to_plan_response(plan: Plan, derived_status: str | None = None) -> PlanResponse:
    return PlanResponse(
        id=plan.id,
        repo=plan.repo,
        intent=plan.intent,
        doc_path=plan.doc_path,
        parent_plan_id=plan.parent_plan_id,
        created_by=plan.created_by,
        created_at=plan.created_at,
        derived_status=derived_status,
    )


async def create_plan_from_doc(
    session: AsyncSession,
    dispatcher: Dispatcher,
    *,
    repo: str,
    doc_content: str,
    doc_path: str | None,
    created_by: str | None,
    plan_id: uuid.UUID | None = None,
) -> Plan:
    """Internal Scenario-1 plan-creation function.

    Used by both ``POST /plans`` (when ``doc_content`` is supplied) and
    the merge-to-main trigger handler (ADR-0021). Parses the doc, INSERTs
    the Plan + Task rows, emits ``PlanRegistered`` + ``PlanActivated`` +
    one ``TaskRegistered`` per task, and dispatches each task.

    ``plan_id`` is optional: when supplied, the Plan row is INSERTed with
    that id; the caller controls the id so merge-trigger redelivery
    converges on the same row (ADR-0021's deterministic-id-from-uuid5
    trick). When ``None``, the DB-side default ``gen_random_uuid()``
    applies. INSERTing with a duplicate ``plan_id`` raises an
    ``IntegrityError`` — callers that need idempotency should probe for
    the existing Plan row before calling.

    The session is **not** committed here. The caller commits (the HTTP
    route commits at the end of the request; the merge handler commits
    once per dispatched plan-doc).

    Raises:
        PlanDocFormatError: the doc has no ``## sequence_of_work`` block
            or its YAML is malformed.
        pydantic.ValidationError: the parsed YAML fails the TaskSpec
            schema (missing fields, extras, etc.).
        HTTPException: 400 when the workflow slug is unknown or the
            depends_on grammar is violated. The merge handler catches
            this; the HTTP route lets FastAPI surface it.
        DispatchError: a downstream dispatch failure. Same handling as
            ``HTTPException`` above.
    """
    specs = parse_plan_doc(doc_content)
    validate_unique_task_ids(specs)
    frontmatter = parse_plan_doc_frontmatter(doc_content)

    plan_kwargs: dict[str, object] = {
        "repo": repo,
        "intent": None,
        "doc_path": doc_path,
        "created_by": created_by,
        "auto_merge": frontmatter.auto_merge,
    }
    if plan_id is not None:
        plan_kwargs["id"] = plan_id
    plan = Plan(**plan_kwargs)
    session.add(plan)
    await session.flush()

    await dispatcher.persist_and_publish(
        session,
        entity_type="plan",
        action="registered",
        payload=PlanRegistered(
            repo=plan.repo,
            intent=plan.intent,
            doc_path=plan.doc_path,
        ),
        plan_id=plan.id,
    )
    await dispatcher.persist_and_publish(
        session,
        entity_type="plan",
        action="activated",
        payload=PlanActivated(doc_path=plan.doc_path),
        plan_id=plan.id,
    )
    tasks = await _spawn_tasks_from_specs(
        session, dispatcher, plan, specs, created_by,
    )
    for task in tasks:
        await dispatcher.dispatch_task(session, task)
    return plan


async def _read_plan_derived_status(
    session: AsyncSession, plan_id: uuid.UUID,
) -> str | None:
    """LEFT JOIN the ``plan_status`` VIEW for a single plan. Returns
    ``None`` if the VIEW has no row (theoretical — the VIEW joins LEFT so
    every plan row appears with at least the ``drafting`` default)."""
    from sqlalchemy import text

    result = await session.execute(
        text("SELECT derived_status FROM plan_status WHERE id = :id"),
        {"id": plan_id},
    )
    row = result.first()
    if row is None:
        return None
    return row.derived_status


async def _auto_seed_starters_on_submit(
    session: AsyncSession,
    settings: Settings,
    dispatcher: Dispatcher,
) -> None:
    """Run the empty-workflows auto-seed branch (post-mortem surprise A).

    Post-mortem of the combined ADR-0085+0086 plan (2026-06-09): a
    fresh DB has no workflows registered; the first
    ``treadmill plan submit`` would 400 with "workflow 'wf-author' not
    registered" until ``treadmill workflows seed-starters`` ran
    manually. This closes the cliff edge by running the same
    ``seed_starters_if_empty`` transaction the startup-side
    ``_auto_seed_starters`` uses when ``wf-author`` is absent.

    Cheap on the happy path — one async EXISTS query against the
    ``workflows`` table; nothing else fires when seeded. On the cold
    branch we run the sync seed via ``asyncio.to_thread`` so the event
    loop isn't blocked, then emit one ``system.auto_seeded_starters``
    event so the dashboard + treadmill-events stream carry an audit
    trail.

    Failure modes:
      * Seed transaction raises → ``HTTPException(500)`` naming
        auto-seed as the cause. Never silently persist a plan against
        an unseeded DB.
      * Seed returns 0 (another replica won the ``SELECT FOR UPDATE``
        race between our EXISTS check and the lock acquisition) → no
        event, no log; the other replica's commit fired one already.
    """
    wf_author_present = await session.scalar(
        select(exists().where(Workflow.id == "wf-author"))
    )
    if wf_author_present:
        return

    from treadmill_api.starters import run_auto_seed_starters_sync

    try:
        seeded_role_count = await asyncio.to_thread(
            run_auto_seed_starters_sync, settings
        )
    except Exception as exc:
        logger.error(
            "plan submit: auto-seed of starter roles failed: %s", exc
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "plan rejected: auto-seed of starter roles failed: "
                f"{exc}"
            ),
        ) from exc

    if seeded_role_count > 0:
        logger.info(
            "plan submit: auto-seeded %d starter roles into fresh DB",
            seeded_role_count,
        )
        await dispatcher.persist_and_publish(
            session,
            entity_type="system",
            action="auto_seeded_starters",
            payload=SystemAutoSeededStarters(
                roles_seeded=seeded_role_count,
                triggered_by="plan_submit",
            ),
        )


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("", response_model=PlanResponse, status_code=status.HTTP_201_CREATED)
async def create_plan(
    body: PlanCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
    settings: Annotated[Settings, Depends(get_settings)],
    team_config_store: Annotated[
        TeamConfigStore, Depends(get_team_config_store)
    ],
) -> PlanResponse:
    """Create a Plan. Scenario 1 (with ``doc_content``) parses the doc and
    spawns Task rows; Scenario 2 (``intent`` only) creates a drafting plan
    that will be activated by a later ``submit-doc`` call.

    Lifecycle events emitted (A.6):
      * Always: ``PlanRegistered``.
      * Scenario 1 only: ``PlanActivated`` in the same transaction
        (decision #4 — the plan doc *is* on hand, so we don't wait for a
        ``submit-doc`` round-trip).
      * One ``TaskRegistered`` per spawned task.

    D.10 — ``body.dev`` short-circuits the ``wf-plan`` PR-merge gate for
    intent-only submissions when
    ``TREADMILL_DEPLOYMENT_MODE=fully_local``: the plan is activated
    inline and a single ``wf-author`` task is spawned with the intent as
    its description. Outside fully_local mode the flag is ignored with a
    logged warning. With ``doc_content`` present, the flag is a no-op —
    the standard path already produces an active plan.
    """
    frontmatter_auto_merge: bool | None = None
    if body.doc_content is not None:
        try:
            specs = parse_plan_doc(body.doc_content)
            validate_unique_task_ids(specs)
            frontmatter_auto_merge = parse_plan_doc_frontmatter(
                body.doc_content
            ).auto_merge
        except (PlanDocFormatError, ValidationError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"plan-doc parse failed: {exc}",
            ) from exc

    await _auto_seed_starters_on_submit(session, settings, dispatcher)

    # ADR-0085+0086 Task D — repo-scoped team_configs lookup.
    # When the repo has a team_configs row AND the request didn't
    # supply ``created_by``, auto-derive it from the team's
    # coordinator label. When the row is absent, behavior is
    # unchanged — the legacy ``body.created_by`` lands verbatim and
    # no ``plan.submitted`` event is emitted at the end.
    team_config = await team_config_store.get_by_repo(session, body.repo)
    if team_config is not None and body.created_by is None:
        body.created_by = team_config.coordinator_label

    # D.10 — resolve the dev fast-path. Honored in any LOCAL mode
    # (fully_local OR dev_local) for intent-only (Scenario 2) submissions;
    # doc-driven Scenario 1 already produces an active plan so the flag
    # is a no-op there. Rejected only in fully_remote, where the wf-plan
    # PR-merge gate is the only governance path.
    #
    # 2026-05-18: extended from fully_local-only to also include dev_local
    # after observing that ``treadmill submit`` (intent-only) in dev_local
    # created inert plans (status=drafting) with no path to activation
    # short of authoring a sequence_of_work-shaped doc and POSTing to
    # /submit-doc. The gate the flag skips is the wf-plan PR-merge gate
    # (a plan-doc PR has to merge before tasks dispatch); that gate is
    # orthogonal to AWS — the moto/real-AWS distinction the original
    # comment cited doesn't actually apply to plan activation. Operators
    # in dev_local who explicitly pass --dev want the same fast-path.
    is_local = (
        settings.deployment_mode == DeploymentMode.FULLY_LOCAL
        or settings.deployment_mode == DeploymentMode.DEV_LOCAL
    )
    dev_active = body.dev and is_local and body.doc_content is None
    if body.dev and not is_local:
        logger.warning(
            "dev flag ignored — fully_remote deployment requires "
            "wf-plan PR-merge gate; running standard plan-creation path",
        )

    plan = Plan(
        repo=body.repo,
        intent=body.intent,
        doc_path=body.doc_path,
        created_by=body.created_by,
        auto_merge=frontmatter_auto_merge,
    )
    session.add(plan)
    await session.flush()

    # PlanRegistered fires for both scenarios.
    await dispatcher.persist_and_publish(
        session,
        entity_type="plan",
        action="registered",
        payload=PlanRegistered(
            repo=plan.repo,
            intent=plan.intent,
            doc_path=plan.doc_path,
        ),
        plan_id=plan.id,
    )

    # Track how many tasks the submission spawned. Used below for the
    # ``plan.submitted`` event payload — coordinator-side fan-out sizing
    # depends on this. Scenarios that don't spawn tasks (Scenario 2
    # standard intent-only) leave it at 0.
    task_count = 0

    if body.doc_content is not None:
        # Scenario 1: doc-driven create. PlanActivated fires in the same
        # transaction (decision #4) before tasks dispatch — Phase 3 D.5's
        # plan-active gate will read this state before unblocking work.
        await dispatcher.persist_and_publish(
            session,
            entity_type="plan",
            action="activated",
            payload=PlanActivated(doc_path=plan.doc_path),
            plan_id=plan.id,
        )
        tasks = await _spawn_tasks_from_specs(
            session, dispatcher, plan, specs, body.created_by,
        )
        try:
            for task in tasks:
                await dispatcher.dispatch_task(session, task)
        except DispatchError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
            ) from exc
        task_count = len(tasks)
    elif dev_active:
        # D.10 — dev fast-path for intent-only submissions in local mode.
        # Skip the wf-plan PR-merge gate: emit PlanActivated inline and
        # spawn an implicit one-task wf-author run with the intent as
        # both the task title and description.
        await dispatcher.persist_and_publish(
            session,
            entity_type="plan",
            action="activated",
            payload=PlanActivated(doc_path=None),
            plan_id=plan.id,
        )
        try:
            implicit_task = await _spawn_dev_wf_author_task(
                session, dispatcher, plan, body.intent or "", body.created_by,
            )
            await dispatcher.dispatch_task(session, implicit_task)
        except DispatchError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
            ) from exc
        task_count = 1

    # ADR-0085+0086 Task D — emit the plan.submitted event so the
    # team's coordinator picks the work up without polling. Only fires when
    # ``team_configs`` has a row for the plan's repo; repos without a
    # row stay on the legacy ``PlanRegistered`` + ``PlanActivated`` shape
    # and never fire this. Lands in the same transaction as the rest of
    # the plan lifecycle events so the coordinator's listener sees the
    # plan + tasks atomically.
    if team_config is not None:
        await dispatcher.persist_and_publish(
            session,
            entity_type="plan",
            action="submitted",
            payload=PlanSubmitted(
                repo=plan.repo,
                coordinator_label=team_config.coordinator_label,
                task_count=task_count,
            ),
            plan_id=plan.id,
        )

    await session.commit()
    await session.refresh(plan)
    derived_status = await _read_plan_derived_status(session, plan.id)
    return _to_plan_response(plan, derived_status)


@router.get("/{plan_id}", response_model=PlanResponse)
async def get_plan(
    plan_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PlanResponse:
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
    derived_status = await _read_plan_derived_status(session, plan_id)
    return _to_plan_response(plan, derived_status)


@router.get("/{plan_id}/tasks", response_model=list[TaskResponse])
async def list_plan_tasks(
    plan_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[TaskResponse]:
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")

    # Inner-join the task_status VIEW so we get derived_status alongside the
    # task fields. The VIEW returns one row per task; LEFT JOIN handles the
    # (theoretical) case where the VIEW filters a task out.
    from sqlalchemy import text

    result = await session.execute(
        text(
            """
            SELECT t.id, t.plan_id, t.repo, t.title, t.description,
                   t.workflow_version_id, t.created_at,
                   ts.derived_status
            FROM tasks t
            LEFT JOIN task_status ts ON ts.id = t.id
            WHERE t.plan_id = :plan_id
            ORDER BY t.created_at
            """
        ),
        {"plan_id": plan_id},
    )
    return [
        TaskResponse(
            id=row.id,
            plan_id=row.plan_id,
            repo=row.repo,
            title=row.title,
            description=row.description,
            workflow_version_id=row.workflow_version_id,
            created_at=row.created_at,
            derived_status=row.derived_status,
        )
        for row in result
    ]


@router.post("/{plan_id}/submit-doc", response_model=PlanResponse)
async def submit_plan_doc(
    plan_id: uuid.UUID,
    body: PlansSubmitDocRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    dispatcher: Annotated[Dispatcher, Depends(get_dispatcher)],
) -> PlanResponse:
    """Attach a plan doc to an existing (drafting) Plan and spawn Tasks
    from its ``## sequence_of_work`` block. Used by the Scenario 2 flow
    when ``wf-plan`` produces the doc post-hoc."""
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
    if plan.doc_path is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="plan already has a doc_path; submit-doc may only be called once",
        )

    try:
        specs = parse_plan_doc(body.doc_content)
        validate_unique_task_ids(specs)
        frontmatter = parse_plan_doc_frontmatter(body.doc_content)
    except (PlanDocFormatError, ValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"plan-doc parse failed: {exc}",
        ) from exc

    plan.doc_path = body.doc_path
    plan.auto_merge = frontmatter.auto_merge
    # Activating an existing drafting plan — emit PlanActivated then
    # spawn + dispatch. Mirrors Scenario 1 ordering inside create_plan.
    await dispatcher.persist_and_publish(
        session,
        entity_type="plan",
        action="activated",
        payload=PlanActivated(doc_path=plan.doc_path),
        plan_id=plan.id,
    )
    tasks = await _spawn_tasks_from_specs(
        session, dispatcher, plan, specs, plan.created_by,
    )
    try:
        for task in tasks:
            await dispatcher.dispatch_task(session, task)
    except DispatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc),
        ) from exc
    await session.commit()
    await session.refresh(plan)
    derived_status = await _read_plan_derived_status(session, plan.id)
    return _to_plan_response(plan, derived_status)
