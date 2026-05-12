"""Canonical starter workflows + roles for a fresh Treadmill install.

Per ADR-0015 (multi-step workflows + role reuse), Treadmill ships eight
roles and seven workflows. Four of the workflows are single-step
(``wf-author``, ``wf-review``, ``wf-validate``) and three plus one are
two-step analyzer-then-action shapes (``wf-plan``, ``wf-feedback``,
``wf-ci-fix``, ``wf-conflict``). The shared terminal is
``role-code-author`` — the same role across ``wf-author``,
``wf-feedback``, ``wf-ci-fix``, and ``wf-conflict``. Specialization
lives in the analyzer's prompt + the structured ``task_directive`` it
produces; the action role sees a uniform input shape regardless of
which workflow it's in.

This module exposes:

  * ``STARTERS`` — the seven canonical workflows + their underlying
    roles, fully declared as plain dicts. ``test_starters.py``
    enforces the content invariants per ADR-0015 §"``starters.py``
    rewrite".

  * ``seed(api_client)`` — POSTs each role + workflow + version to the
    existing CRUD endpoints, swallowing 409s so re-runs are idempotent.
    Returns the count of *newly created* workflows (409s don't count).

The planner is the only role on the expensive opus tier per ADR-0015
§"Trade-offs". All other roles (including the analyzers) run on the
cheap haiku tier — analyzer cost is the rationale for splitting
analyzer from action in the first place.
"""

from __future__ import annotations

from typing import Any, Protocol

from treadmill_api.models import OutputKind


# Model identifiers — kept as a small constant so the test can assert
# the planner is the expensive model and the others share the cheap one.
PLANNER_MODEL = "claude-opus-4-7"
WORKER_MODEL = "claude-haiku-4-5-20251001"


# ── Role definitions ─────────────────────────────────────────────────────────

# Each ``system_prompt`` below is the full role-specific prompt authored
# in C.3 per ADR-0015 §"Role taxonomy" + ADR-0012 §"Decision-string
# value-sets per workflow". Every prompt names:
#
#   * the role + workflow context,
#   * its input contract (what it sees in the prompt),
#   * its output contract (the uniform ``StepOutput`` envelope from
#     ADR-0012 — ``summary`` / ``decision`` / ``commit_sha`` / ``artifacts``
#     / ``payload``) with the explicit decision value-set,
#   * the action it performs (which tools / commands to run).
#
# Analyzer roles produce a ``task_directive`` in ``payload.task_directive``
# (the analyzer→action contract per ADR-0015 §"``task_directive``"). The
# shared terminal ``role-code-author`` consumes either a task spec
# (single-step ``wf-author``) or a ``task_directive`` from
# ``prior_steps[-1]`` (multi-step shapes).

_ROLES: list[dict[str, Any]] = [
    {
        "id": "role-planner",
        "model": PLANNER_MODEL,
        "output_kind": OutputKind.ANALYSIS,
        "system_prompt": (
            "You are the Treadmill planner — analyzer step of "
            "``wf-plan``. Input: a free-text intent plus read-only "
            "access to the repo. Action: research the codebase enough "
            "to produce a plan-doc-task-spec-shaped ``task_directive`` "
            "(id, title, workflow, intent, ``scope.files``, "
            "``validation``) for the downstream doc author; do NOT edit "
            "files. Output the uniform ``StepOutput`` envelope per "
            "ADR-0012: ``summary`` is a one-line description of the "
            "planned change; ``decision`` is ``plan-ready`` (complete "
            "directive) or ``blocked`` (human input needed — explain "
            "in ``summary``); ``payload.task_directive`` carries the "
            "directive; ``commit_sha`` stays null (no commit yet)."
        ),
    },
    {
        "id": "role-doc-author",
        "model": WORKER_MODEL,
        "output_kind": OutputKind.PLAN_DOC,
        "system_prompt": (
            "You are the Treadmill plan-doc author — action step of "
            "``wf-plan``. Input: the planner's ``task_directive`` at "
            "``prior_steps[-1].output.payload.task_directive``. Action: "
            "author a plan doc at ``docs/plans/<date>-<slug>.md`` per "
            "ADR-0010 + ADR-0003, check out a ``plan/<plan-id>-<slug>`` "
            "branch (ADR-0010 §\"Branch conventions\"), commit, push, "
            "open a PR with ``gh pr create``. Stay within the "
            "directive's scope; do not invent new tasks. Output the "
            "uniform ``StepOutput`` envelope per ADR-0012: ``summary`` "
            "is a one-line note of what landed; ``decision`` is "
            "``plan-doc-pushed`` (PR open) or ``blocked`` (explain in "
            "``summary``); ``artifacts`` carries ``pr_url``, "
            "``branch``, and ``doc_path``; ``commit_sha`` top-level is "
            "the SHA you committed."
        ),
    },
    {
        "id": "role-code-author",
        "model": WORKER_MODEL,
        "output_kind": OutputKind.CODE,
        "system_prompt": (
            "You are the Treadmill code author — the shared terminal "
            "for ``wf-author``, ``wf-feedback``, ``wf-ci-fix``, "
            "``wf-conflict``. Input: either a task spec (single-step "
            "``wf-author``) or a ``task_directive`` at "
            "``prior_steps[-1].output.payload.task_directive`` (the "
            "multi-step shapes). Action: edit files, run the project's "
            "tests, commit (the runner appends "
            "``Treadmill-Task-Id`` / ``Treadmill-Step-Id`` trailers — "
            "write a clear subject), push, open the PR with "
            "``gh pr create`` (first push only; later pushes update). "
            "SCOPE DISCIPLINE: only modify files in ``scope.files`` "
            "(or the directive's ``files``); files in ``out_of_scope`` "
            "are explicit guards — never touch them. For "
            "``wf-feedback``'s no-code branch, post a PR comment with "
            "``gh pr comment`` instead of pushing. Output the uniform "
            "``StepOutput`` envelope per ADR-0012: ``summary`` is a "
            "one-line headline; ``decision`` is one of ``pushed`` / "
            "``no-changes`` / ``blocked`` (``wf-author``), "
            "``fix-pushed`` / ``gave-up`` / ``not-our-bug`` "
            "(``wf-ci-fix``), ``code-change-dispatched`` / "
            "``responded-without-change`` / ``blocked`` "
            "(``wf-feedback``), ``resolved`` / ``gave-up`` "
            "(``wf-conflict``); ``artifacts`` carry ``branch`` and "
            "``pr_url``; ``payload.pr_number`` is the GitHub PR "
            "number; ``commit_sha`` top-level is the SHA you committed."
        ),
    },
    {
        "id": "role-reviewer",
        "model": WORKER_MODEL,
        "output_kind": OutputKind.REVIEW,
        "system_prompt": (
            "You are the Treadmill reviewer — single step of "
            "``wf-review``. Input: the PR diff (fetch via "
            "``gh pr diff <number>``), the task's ``scope`` + "
            "``intent``, the plan intent, and project ADRs under "
            "``docs/adrs/``. Action: judge the diff against scope + "
            "intent + relevant ADRs, post per-file comments with "
            "``gh pr review`` and capture each comment URL. Output the "
            "uniform ``StepOutput`` envelope per ADR-0012: ``summary`` "
            "is a one-line verdict; ``decision`` is ``approved`` / "
            "``changes_requested`` / ``needs-more-info``; "
            "``artifacts`` carry ``comment_id`` entries (one per "
            "review comment URL); ``payload.comments`` lists per-file "
            "feedback; ``commit_sha`` top-level MUST be the PR HEAD "
            "SHA you reviewed — ADR-0013's mergeability VIEW joins on "
            "this field, so a missing/wrong SHA makes the review "
            "invisible to merge eligibility.\n\n"
            "When you have completed your review, end your response "
            "with a line of the form:\n"
            "  ``VERDICT: approve`` — code is acceptable as-is\n"
            "  ``VERDICT: request_changes`` — material problems exist; "
            "the PR should not merge\n"
            "  ``VERDICT: comment`` — observations only; no merge gate\n"
            "If you don't include a VERDICT line, your review defaults "
            "to ``comment``. Per ADR-0022, the worker greps your output "
            "for the last matching VERDICT line and uses it to drive "
            "``gh pr review --approve`` / ``--request-changes`` / "
            "``--comment``."
        ),
    },
    {
        "id": "role-validator",
        "model": WORKER_MODEL,
        # Per ADR-0022 §"Migration of seeded roles" — classified as
        # ``analysis`` placeholder at v0. The Ralph-loop validation ADR
        # (forthcoming) will reclassify this role or move it to a
        # non-Claude-Code runner path entirely.
        "output_kind": OutputKind.ANALYSIS,
        "system_prompt": (
            "You are the Treadmill validator — single step of "
            "``wf-validate``. Input: the task's ``validation`` entries "
            "(persisted in ``task_validations``), the repo at HEAD, "
            "and the PR diff via ``gh pr diff``. Per entry: if "
            "``kind=deterministic``, emit "
            "``{validation_id, status: 'pass', rationale: "
            "'deterministic validation stub — Phase-4 rule engine "
            "will execute'}`` (the v0 stub never emits ``fail``). If "
            "``kind=llm-judge``, evaluate the diff against the entry's "
            "``description`` as the criterion and emit ``status`` of "
            "``pass`` / ``fail`` / ``error`` plus a rationale. Output "
            "the uniform ``StepOutput`` envelope per ADR-0012: "
            "``summary`` is a one-line headline; top-level ``decision`` "
            "is the aggregate — ``pass`` only when every entry passed "
            "(or stub-passed), ``fail`` if any failed, ``error`` if "
            "any errored; ``payload.validation_results`` is the "
            "per-entry list; ``commit_sha`` top-level MUST be the PR "
            "HEAD SHA — ADR-0013's VIEW joins on this field, so an "
            "absent SHA makes the validation invisible to merge "
            "eligibility."
        ),
    },
    {
        "id": "role-feedback-analyzer",
        "model": WORKER_MODEL,
        "output_kind": OutputKind.ANALYSIS,
        "system_prompt": (
            "You are the Treadmill feedback analyzer — analyzer step "
            "of ``wf-feedback``. Input: the inbound PR review comments "
            "(fetch with ``gh pr view`` / ``gh api``) plus the task's "
            "``scope`` + ``intent``. Action: classify each comment as "
            "code-change-required, discussion (response-only), or "
            "blocker-resolved. If any comment requires code, produce a "
            "``task_directive`` (id, title, intent, ``files``, "
            "``out_of_scope``) for ``role-code-author``. Do NOT edit "
            "files — your output is a directive. Output the uniform "
            "``StepOutput`` envelope per ADR-0012: ``summary`` is a "
            "one-line classification headline; ``decision`` is "
            "``plan-ready`` (code change required), ``no-action-needed`` "
            "(responsive-only), or ``blocked`` (human input needed); "
            "``payload.task_directive`` carries the directive; "
            "``payload.classification_summary`` explains the per-comment "
            "classification."
        ),
    },
    {
        "id": "role-ci-analyzer",
        "model": WORKER_MODEL,
        "output_kind": OutputKind.ANALYSIS,
        "system_prompt": (
            "You are the Treadmill CI-failure analyzer — analyzer step "
            "of ``wf-ci-fix``. Input: the failing check name + URL + "
            "its logs (fetch with ``gh run view --log-failed "
            "<run-id>``). Action: identify the failure type (test "
            "failure / lint / type-check / build / other) and the "
            "smallest fix — which file to edit, what change. Produce a "
            "``task_directive`` for ``role-code-author``. Do NOT edit "
            "files — your output is a directive. Output the uniform "
            "``StepOutput`` envelope per ADR-0012: ``summary`` is a "
            "one-line headline; ``decision`` is ``plan-ready`` (fix "
            "clear — directive attached), ``not-our-bug`` "
            "(infrastructure / flake / external), or ``blocked`` "
            "(insufficient info); ``payload.failure_kind`` is the "
            "failure-type label; ``payload.task_directive`` carries "
            "the directive when ``decision=plan-ready``."
        ),
    },
    {
        "id": "role-conflict-analyzer",
        "model": WORKER_MODEL,
        "output_kind": OutputKind.ANALYSIS,
        "system_prompt": (
            "You are the Treadmill conflict analyzer — analyzer step "
            "of ``wf-conflict``. Input: the conflict tree against "
            "main. Run ``git fetch origin main`` then "
            "``git rebase origin/main`` and inspect "
            "``git diff --name-only --diff-filter=U`` for the "
            "conflicted files (use ``git status`` for context). For "
            "each conflicted file, decide the resolution direction: "
            "``prefer task intent`` (keep task changes), "
            "``prefer main`` (defer to upstream), or "
            "``mechanical merge`` (both sides combine cleanly). "
            "Produce a ``task_directive`` with per-file resolution "
            "for ``role-code-author``. Do NOT edit files — your output "
            "is a directive. Output the uniform ``StepOutput`` envelope "
            "per ADR-0012: ``summary`` is a one-line headline; "
            "``decision`` is ``plan-ready`` (resolution clear — "
            "directive attached) or ``blocked`` (too complex — human "
            "input needed); ``payload.task_directive`` carries the "
            "directive; ``payload.conflict_files`` lists conflicted "
            "files."
        ),
    },
]


# Lookup helper for ``STARTERS`` — keeps the workflow definitions below
# concise + the role-by-id assembly DRY.
_ROLES_BY_ID: dict[str, dict[str, Any]] = {r["id"]: r for r in _ROLES}


def _roles_for(*role_ids: str) -> list[dict[str, Any]]:
    """Return the role dicts for ``role_ids`` in caller order, preserving
    the analyzer-then-action sequence used by the two-step workflows."""
    return [_ROLES_BY_ID[rid] for rid in role_ids]


# ── Workflow definitions ─────────────────────────────────────────────────────

# Per ADR-0015 §"Per-workflow shape matrix":
#
#   * ``wf-author`` / ``wf-review`` / ``wf-validate`` are single-step.
#   * ``wf-plan`` / ``wf-feedback`` / ``wf-ci-fix`` / ``wf-conflict`` are
#     two-step analyzer-then-action. The analyzer's step names are
#     workflow-specific (``research`` for wf-plan, ``analyzer`` for the
#     resolution workflows); the action step's name follows the
#     matrix as well (``plan-author`` / ``action``).
#
# ``role-code-author`` is the shared terminal — referenced by exactly
# four workflows (wf-author, wf-feedback, wf-ci-fix, wf-conflict).

STARTERS: list[dict[str, Any]] = [
    {
        "id": "wf-author",
        "description": "Author code changes for a task and open a PR.",
        "roles": _roles_for("role-code-author"),
        "steps": [
            {"name": "author", "role_id": "role-code-author"},
        ],
    },
    {
        "id": "wf-plan",
        "description": "Research an intent and author a plan doc PR.",
        "roles": _roles_for("role-planner", "role-doc-author"),
        "steps": [
            {"name": "research", "role_id": "role-planner"},
            {"name": "plan-author", "role_id": "role-doc-author"},
        ],
    },
    {
        "id": "wf-review",
        "description": "Review the task's PR and emit a decision.",
        "roles": _roles_for("role-reviewer"),
        "steps": [
            {"name": "review", "role_id": "role-reviewer"},
        ],
    },
    {
        "id": "wf-validate",
        "description": "Run the task's declared validation entries.",
        "roles": _roles_for("role-validator"),
        "steps": [
            {"name": "validate", "role_id": "role-validator"},
        ],
    },
    {
        "id": "wf-feedback",
        "description": "Analyze PR review comments and dispatch follow-up work.",
        "roles": _roles_for("role-feedback-analyzer", "role-code-author"),
        "steps": [
            {"name": "analyzer", "role_id": "role-feedback-analyzer"},
            {"name": "action", "role_id": "role-code-author"},
        ],
    },
    {
        "id": "wf-ci-fix",
        "description": "Analyze a failing CI check and push a fix.",
        "roles": _roles_for("role-ci-analyzer", "role-code-author"),
        "steps": [
            {"name": "analyzer", "role_id": "role-ci-analyzer"},
            {"name": "action", "role_id": "role-code-author"},
        ],
    },
    {
        "id": "wf-conflict",
        "description": "Analyze merge conflicts against main and push a resolution.",
        "roles": _roles_for("role-conflict-analyzer", "role-code-author"),
        "steps": [
            {"name": "analyzer", "role_id": "role-conflict-analyzer"},
            {"name": "action", "role_id": "role-code-author"},
        ],
    },
]


# ── Seeding ──────────────────────────────────────────────────────────────────


class _SeedClient(Protocol):
    """The subset of ``treadmill_cli.api_client.ApiClient`` ``seed`` needs."""

    def _request(self, method: str, path: str, **kwargs: Any) -> Any: ...


class StarterSeedError(Exception):
    """Raised when seeding fails for a reason other than 409 conflicts.

    409s are swallowed silently — the install is already partly seeded
    and we want re-runs to be no-ops. Anything else (400, 500, network)
    surfaces so the operator can investigate.
    """


def _all_roles() -> list[dict[str, Any]]:
    """De-duplicate the roles referenced by the starters.

    ``role-code-author`` is referenced by four workflows; this helper
    collapses repeated references so ``seed()`` POSTs each role exactly
    once. The dedup checks reference identity *and* equality — the
    ``_roles_for`` helper above hands out the same dict from
    ``_ROLES_BY_ID`` so identity holds, but the equality test catches
    accidental future inconsistencies.
    """
    seen: dict[str, dict[str, Any]] = {}
    for wf in STARTERS:
        for role in wf["roles"]:
            seen.setdefault(role["id"], role)
    return list(seen.values())


_DEFAULT_EVENT_TRIGGERS: list[tuple[str, str]] = [
    # (event_type, workflow_id) — per Week-3 plan §C.2. ``pr_synchronize``
    # appears once here; the trigger evaluator fans out concurrently to
    # ``wf-validate`` per ``triggers.py:_EXTRA_FANOUT_WORKFLOWS``.
    ("pr_opened", "wf-review"),
    ("pr_synchronize", "wf-review"),
    ("pr_review_submitted", "wf-feedback"),
    ("check_run_completed", "wf-ci-fix"),
    ("pr_conflict", "wf-conflict"),
]


class WorkflowShapeError(StarterSeedError):
    """Raised by ``_validate_workflow_shapes`` when a seeded workflow
    composes its steps in a way that ADR-0022's per-kind dispatch can't
    serve at run time.

    A best-effort static check at v0 — the run-time worker still raises
    on misuse (e.g. a review-kind step against a task that hasn't opened
    a PR yet). Static rejection is the cheaper feedback loop.
    """


def _validate_workflow_shapes() -> None:
    """Reject mis-composed workflow step lists per ADR-0022.

    Three best-effort rules at v0:

      1. A ``review``-kind step can't be the first step of a workflow
         that *opens* the PR — wf-author opens the PR in its first
         step, so it would have nothing to review yet. Equivalent
         shape: any workflow whose first step is a ``review`` role.
      2. A ``plan_doc``-kind step only appears in ``wf-plan``. The
         path-confinement constraint (diff under ``docs/plans/``) is
         workflow-specific.
      3. Every step's role exists in the global roles list. The seed
         function POSTs roles before workflows; an unresolved
         reference would 400 at POST time but it's better to raise
         here with a clean error than to wait for the network round-trip.

    Stronger compile-time validation (orphan-analysis detection, full
    analyzer→action wiring checks) is a future cleanup.
    """
    role_kinds: dict[str, OutputKind] = {
        role["id"]: role["output_kind"] for role in _all_roles()
    }
    for wf in STARTERS:
        steps = wf["steps"]
        if not steps:
            continue
        # Rule 3: every step's role resolves.
        for step in steps:
            if step["role_id"] not in role_kinds:
                raise WorkflowShapeError(
                    f"workflow {wf['id']!r} step {step['name']!r} references "
                    f"undefined role {step['role_id']!r}"
                )
        # Rule 1: first step can't be a review-kind role (review needs
        # a PR; if this workflow is the one that opens the PR, the
        # review has nothing to look at).
        first_kind = role_kinds[steps[0]["role_id"]]
        if first_kind is OutputKind.REVIEW and wf["id"] != "wf-review":
            # ``wf-review`` is fired by ``pr_opened`` (a PR already
            # exists at trigger time), so a review-first composition
            # is fine there. Any other workflow that opens with a
            # review step is the misuse this rule catches.
            raise WorkflowShapeError(
                f"workflow {wf['id']!r} starts with a review-kind step "
                f"({steps[0]['role_id']!r}); a review needs an existing PR, "
                "so review-first composition is only valid for workflows "
                "fired by PR-existence events (today, just wf-review)."
            )
        # Rule 2: plan_doc only in wf-plan.
        for step in steps:
            kind = role_kinds[step["role_id"]]
            if kind is OutputKind.PLAN_DOC and wf["id"] != "wf-plan":
                raise WorkflowShapeError(
                    f"workflow {wf['id']!r} step {step['name']!r} uses a "
                    f"plan_doc-kind role ({step['role_id']!r}); the "
                    "docs/plans/ confinement constraint is wf-plan-specific."
                )


def seed(api_client: _SeedClient) -> int:
    """Seed the starter workflows + roles via the API CRUD endpoints.

    Idempotent: each POST that returns 409 is treated as already-seeded
    and silently skipped. Returns the count of *workflows freshly
    created* on this call (does not include roles or versions).

    Also ensures the five default ``event_triggers`` catch-all rows
    exist (per Week-3 plan §C.2). Alembic migration ``0007`` is the
    primary seeder for these, but on a fresh install the migration
    skips them because the workflows don't exist yet (FK constraint).
    Re-running ``seed()`` after the workflow POSTs closes that gap.
    Both paths are idempotent.
    """
    from treadmill_cli.api_client import ApiError  # local import for protocol decoupling

    # Best-effort static check (ADR-0022): reject mis-composed workflows
    # before we touch the network. A misuse caught here saves the
    # operator a partially-seeded install + a retry.
    _validate_workflow_shapes()

    fresh_workflow_count = 0

    # Roles first — workflows reference them by id.
    for role in _all_roles():
        try:
            api_client._request(
                "POST", "/api/v1/roles",
                json={
                    "id": role["id"],
                    "model": role["model"],
                    "system_prompt": role["system_prompt"],
                    # Per ADR-0022 — every role declares its output kind
                    # so the runner's per-kind dispatch can pick the
                    # right disposition handler. ``OutputKind`` is a
                    # ``StrEnum`` so its value is wire-safe (lowercase
                    # snake_case per ADR-0016).
                    "output_kind": role["output_kind"].value,
                    "skills": [],
                    "hooks": [],
                },
            )
        except ApiError as exc:
            if exc.status_code == 409:
                continue
            raise StarterSeedError(
                f"seeding role {role['id']!r} failed: {exc.detail}"
            ) from exc

    # Workflows + their v1 version.
    #
    # Versions auto-increment server-side (each POST yields v1, v2, …) so
    # we cannot blindly re-POST on every seed run — that would inflate the
    # version count. Instead, GET the workflow first; the response carries
    # ``latest_version``. Only POST a version when there isn't one.
    for wf in STARTERS:
        created = False
        try:
            api_client._request(
                "POST", "/api/v1/workflows",
                json={"id": wf["id"], "description": wf["description"]},
            )
            created = True
        except ApiError as exc:
            if exc.status_code != 409:
                raise StarterSeedError(
                    f"seeding workflow {wf['id']!r} failed: {exc.detail}"
                ) from exc

        # Inspect current state before creating a new version.
        try:
            current = api_client._request("GET", f"/api/v1/workflows/{wf['id']}")
        except ApiError as exc:
            raise StarterSeedError(
                f"inspecting {wf['id']!r} after seed failed: {exc.detail}"
            ) from exc

        if current.get("latest_version") is None:
            try:
                api_client._request(
                    "POST", f"/api/v1/workflows/{wf['id']}/versions",
                    json={"steps": wf["steps"]},
                )
            except ApiError as exc:
                raise StarterSeedError(
                    f"seeding {wf['id']!r} v1 failed: {exc.detail}"
                ) from exc

        if created:
            fresh_workflow_count += 1

    # Default event_triggers — catch-all rows per Week-3 plan §C.2.
    # 409 means a row already exists (either the migration seeded it or
    # an earlier seed run did); silently skip and move on. Any other
    # error is a real bug and surfaces.
    for event_type, workflow_id in _DEFAULT_EVENT_TRIGGERS:
        try:
            api_client._request(
                "POST", "/api/v1/event-triggers",
                json={
                    "repo": None,
                    "event_type": event_type,
                    "workflow_id": workflow_id,
                    "version_strategy": "latest",
                    "enabled": True,
                },
            )
        except ApiError as exc:
            if exc.status_code == 409:
                continue
            raise StarterSeedError(
                f"seeding event_trigger ({event_type} → {workflow_id}) "
                f"failed: {exc.detail}"
            ) from exc

    return fresh_workflow_count
