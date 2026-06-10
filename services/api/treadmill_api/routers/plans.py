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

import logging
import re
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.config import Settings, get_settings
from treadmill_api.dependencies_db import get_session
from treadmill_api.dispatch import Dispatcher, get_dispatcher
from treadmill_api.events.plan import (
    PlanActivated,
    PlanRegistered,
    PlanSubmitted,
)
from treadmill_api.events.task import TaskRegistered
from treadmill_api.models import (
    Plan,
    Task,
    TaskDependency,
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

    A ``team_configs`` row for the plan's repo is required; the request
    412s without one — run ``treadmill team up --repo <slug>`` first.
    """

    repo: str = Field(..., min_length=1)
    intent: str | None = None
    doc_path: str | None = None
    doc_content: str | None = None
    created_by: str | None = None

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
    workflow_version_id: uuid.UUID | None = None
    """Always null post-ADR-0087 Phase 5 — tasks no longer pin a
    workflow version (the coordinator decides execution at dispatch
    time). Field kept for wire-compat one deprecation window."""
    created_at: datetime
    derived_status: str | None = None


class PlansSubmitDocRequest(BaseModel):
    doc_path: str
    doc_content: str = Field(..., min_length=1)


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _spawn_tasks_from_specs(
    session: AsyncSession,
    dispatcher: Dispatcher,
    plan: Plan,
    specs: list[TaskSpec],
    created_by: str | None,
) -> list[Task]:
    """Translate the parsed TaskSpec list into Task rows.

    Post-ADR-0087 Phase 5, a spec's ``workflow:`` field is accepted for
    back-compat (older plan docs carry ``workflow: wf-author``) but
    ignored — tasks no longer pin a workflow version; the coordinator
    decides how a task executes at dispatch time.

    Side effects beyond ``tasks`` INSERTs:

      * Persist + publish a ``TaskRegistered`` event per task (A.6).
      * INSERT ``task_dependencies`` rows after sibling-id → UUID
        substitution + grammar validation (D.1).
    """
    # Pass 1: create task rows and remember the sibling-id → UUID map so
    # the dependency-expression rewriter in pass 2 can substitute. We can't
    # write task_dependencies in pass 1 because a sibling might not have a
    # UUID yet when its dependant is processed.
    tasks: list[Task] = []
    sibling_id_to_uuid: dict[str, uuid.UUID] = {}
    spec_by_task_id: dict[uuid.UUID, TaskSpec] = {}
    for spec in specs:
        task = Task(
            plan_id=plan.id,
            repo=plan.repo,
            title=spec.title,
            description=spec.intent,
            created_by=created_by,
        )
        session.add(task)
        tasks.append(task)
        await session.flush()  # produces task.id
        sibling_id_to_uuid[spec.id] = task.id
        spec_by_task_id[task.id] = spec

    # Pass 2: persist dependencies + emit lifecycle events.
    #
    # task_validations INSERTs removed per ADR-0087 Phase 4 — the table
    # is dropped; the evaluator's holistic PR judgment replaces per-task
    # validation gates (ADR-0029 superseded). Plan-doc ``validation:``
    # blocks still parse (the spec shape is unchanged) and flow to the
    # worker via the coordinator's brief; they are no longer persisted.
    for task in tasks:
        spec = spec_by_task_id[task.id]
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
                plan_id=plan.id,
            ),
            plan_id=plan.id,
            task_id=task.id,
        )

    return tasks



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
    one ``TaskRegistered`` per task. Tasks stay in ``registered``
    state; the coordinator picks them up via ``task.registered`` WS events.

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

    Requires a ``team_configs`` row for the plan's repo (412 otherwise).
    Tasks stay in ``registered`` state after creation; the coordinator
    picks them up via ``plan.submitted`` WS events.

    Lifecycle events emitted (A.6):
      * Always: ``PlanRegistered`` + ``plan.submitted``.
      * Scenario 1 only: ``PlanActivated`` in the same transaction.
      * One ``TaskRegistered`` per spawned task.
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


    # ADR-0087 — a team_configs row is required. The coordinator discovers
    # plans via the coordinator_label field in the plan.submitted event.
    team_config = await team_config_store.get_by_repo(session, body.repo)
    if team_config is None:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail=(
                f"no team configured for repo {body.repo!r} — "
                "run: treadmill team up --repo <slug>"
            ),
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
        # transaction (decision #4) — Phase 3 D.5's plan-active gate reads
        # this state before unblocking work.
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
        task_count = len(tasks)

    # ADR-0087 — emit plan.submitted so the coordinator picks up the work.
    # Lands in the same transaction; coordinator sees plan + tasks atomically.
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
                   t.created_at,
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
    await session.commit()
    await session.refresh(plan)
    derived_status = await _read_plan_derived_status(session, plan.id)
    return _to_plan_response(plan, derived_status)
