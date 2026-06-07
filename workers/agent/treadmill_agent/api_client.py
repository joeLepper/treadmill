"""Worker → API client.

A worker only reads from the API: it fetches the WorkerContext for a
step. Status updates flow the other way (worker → SNS → coordination
consumer → DB) per ADR-0011 and the user's "fully event-driven" rule —
no synchronous HTTP write path from worker to API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    content: str


@dataclass(frozen=True)
class Hook:
    id: str
    name: str
    event: str
    matcher: str | None
    command: str


@dataclass(frozen=True)
class Role:
    id: str
    model: str
    system_prompt: str
    output_kind: str
    """Per ADR-0022 — one of ``code`` / ``review`` / ``analysis`` /
    ``plan_doc``. The runner's per-kind dispatch table reads this to
    pick the right post-Claude-Code disposition handler. Carried as a
    bare ``str`` rather than an enum so the worker stays decoupled
    from the API's SQLAlchemy enum machinery — the dispatch table is
    keyed by string."""
    skills: list[Skill]
    hooks: list[Hook]
    # ``compute_tier`` was dropped from the wire in the Week 2 closure
    # (closure plan decision #12). The DB column stays as forward-compat
    # ballast; the worker no longer reads it.


@dataclass(frozen=True)
class PriorStep:
    """A completed prior step in the same run, exposed in the worker
    context per ADR-0015. The worker's prompt-composer folds these into
    the role's input for multi-step workflows (e.g. ``wf-feedback`` step
    2 reads ``prior_steps[0].output['payload']['task_directive']`` per
    ADR-0015 + ADR-0012 conventions).

    ``output`` mirrors what the API returns: a raw dict (the StepOutput
    envelope per ADR-0012 once A.2 lands; the closure-plan union shape
    before). Either way, the worker reads it conventionally — no static
    typing at this boundary.
    """

    step_index: int
    step_name: str
    role_id: str
    status: str
    output: dict[str, Any] | None


@dataclass(frozen=True)
class SourceStep:
    """The upstream step whose completion triggered this run.

    Populated by the API when ``run.source_step_id`` is set. Today only
    the architect-amend → wf-feedback dispatch sets it: the
    wf-feedback analyzer reads
    ``source_step.output['payload']['remediation_summary']`` (+
    ``['reasoning']``) and honors the architect's directive verbatim
    instead of re-evaluating the original failure from scratch.

    Distinct from ``prior_steps`` — that field is the intra-run
    analyzer→action communication path (ADR-0015 §``task_directive``).
    ``source_step`` is the cross-run / cross-workflow plumbing.

    ``output`` mirrors the raw JSONB envelope (per ADR-0011 +
    ADR-0012); the prompt composer dispatches on ``workflow_id`` to
    know what shape to expect in ``payload``.
    """

    step_id: str
    run_id: str
    workflow_id: str
    step_name: str
    output: dict[str, Any] | None


@dataclass(frozen=True)
class TaskValidationInfo:
    """A validation check from the task's ``validation:`` block.

    Per the 2026-05-14 learning, the code disposition runs deterministic
    checks (via ``validation_runtime.run_deterministic``) before pushing
    to gate on author-side self-validation.
    """

    id: str
    kind: str
    description: str
    script: str | None
    prompt: str | None


@dataclass(frozen=True)
class WorkerContext:
    """Decoded GET /api/v1/steps/{id} response."""

    step_id: str
    run_id: str
    step_index: int
    step_name: str
    status: str

    task_id: str
    plan_id: str
    repo: str
    title: str
    description: str | None

    plan_intent: str | None
    plan_doc_path: str | None

    workflow_id: str
    workflow_version: int
    trigger: str

    role: Role
    pr_number: int | None
    """Per ADR-0022 — the PR number this step relates to, derived
    server-side from ``task_prs`` when one exists. Required for
    ``review``-kind steps; the dispatch handler raises
    ``MissingContextError`` when absent."""
    prior_steps: list[PriorStep]
    task_validations: list[TaskValidationInfo] = field(default_factory=list)
    """Task-specific validation checks from the plan-doc task spec.
    Per the 2026-05-14 learning, the code disposition runs deterministic
    checks before pushing to gate on author-side self-validation. Defaults
    to empty so callers that don't have validation info (e.g. unit tests
    constructing WorkerContext directly, or the dry-run path) don't need
    to thread an empty list everywhere."""
    source_step: SourceStep | None = None
    """The upstream step whose completion triggered this run, when this
    run was dispatched as a self-trigger side-effect. Populated by the
    API when ``run.source_step_id`` is set — today only the architect-
    amend → wf-feedback path. The prompt composer reads
    ``source_step.output`` for the upstream directive; the analyzer
    honors it verbatim instead of re-evaluating. ``None`` for paths
    that didn't plumb cross-run context (initial dispatch, webhook
    fan-out, etc.)."""
    operator_note: str | None = None
    """Per ADR-0081: operator-injected hint for the worker. When non-null
    and the repo's worker_hints_enabled is true, the worker injects this
    into the system prompt before invoking Claude Code. Last in the
    field order so it can default-None without breaking earlier
    non-default fields."""


class ApiClient:
    """Thin httpx wrapper. One client per worker process; reuses
    connection pool across the (few) requests in a worker's lifetime."""

    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ApiClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def fetch_step_context(self, step_id: str) -> WorkerContext:
        resp = self._client.get(f"/api/v1/steps/{step_id}")
        resp.raise_for_status()
        return _decode_context(resp.json())


def _decode_context(body: dict[str, Any]) -> WorkerContext:
    role_block = body["role"]
    # ``source_step`` is present when the API resolved a non-NULL
    # ``run.source_step_id`` (today: architect-amend → wf-feedback).
    # Older API responses + the (vast majority of) dispatch paths that
    # don't plumb cross-run context omit the key entirely; we accept
    # both ``None`` and absent gracefully so the worker is forward-
    # compatible with any pre-plumbing API mocks still in fixtures.
    source_step_raw = body.get("source_step")
    source_step: SourceStep | None
    if source_step_raw is None:
        source_step = None
    else:
        source_step = SourceStep(
            step_id=source_step_raw["step_id"],
            run_id=source_step_raw["run_id"],
            workflow_id=source_step_raw["workflow_id"],
            step_name=source_step_raw["step_name"],
            output=source_step_raw.get("output"),
        )
    return WorkerContext(
        step_id=body["step"]["id"],
        run_id=body["step"]["run_id"],
        step_index=body["step"]["step_index"],
        step_name=body["step"]["step_name"],
        status=body["step"]["status"],
        task_id=body["task"]["id"],
        plan_id=body["plan"]["id"],
        repo=body["task"]["repo"],
        title=body["task"]["title"],
        description=body["task"]["description"],
        plan_intent=body["plan"]["intent"],
        plan_doc_path=body["plan"]["doc_path"],
        workflow_id=body["run"]["workflow_id"],
        workflow_version=body["run"]["workflow_version"],
        trigger=body["run"]["trigger"],
        role=Role(
            id=role_block["id"],
            model=role_block["model"],
            system_prompt=role_block["system_prompt"],
            # ``output_kind`` is required server-side; default to
            # ``"code"`` here only so a pre-ADR-0022 API response
            # (older test fixture mock) still decodes without
            # crashing. Real workers see the field populated.
            output_kind=role_block.get("output_kind", "code"),
            skills=[
                Skill(id=s["id"], name=s["name"], content=s["content"])
                for s in role_block["skills"]
            ],
            hooks=[
                Hook(
                    id=h["id"], name=h["name"], event=h["event"],
                    matcher=h["matcher"], command=h["command"],
                )
                for h in role_block["hooks"]
            ],
        ),
        # ``pr_number`` is null when the task has no PR row yet
        # (e.g. wf-author hasn't opened one). Per-kind handler enforces.
        pr_number=body.get("pr_number"),
        # ``prior_steps`` was introduced for the ADR-0015 multi-step
        # workflows; the API defaults it to ``[]`` so older payloads
        # (and single-step runs) decode cleanly. ``.get()`` keeps
        # the worker forward-compatible with any pre-A.4 mocks still
        # floating around tests.
        prior_steps=[
            PriorStep(
                step_index=ps["step_index"],
                step_name=ps["step_name"],
                role_id=ps["role_id"],
                status=ps["status"],
                output=ps.get("output"),
            )
            for ps in body.get("prior_steps", [])
        ],
        task_validations=[
            TaskValidationInfo(
                id=v["id"],
                kind=v["kind"],
                description=v["description"],
                script=v.get("script"),
                prompt=v.get("prompt"),
            )
            for v in body.get("task_validations", [])
        ],
        source_step=source_step,
        operator_note=body["task"].get("operator_note"),
    )
