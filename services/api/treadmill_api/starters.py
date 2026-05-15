"""Canonical starter workflows + roles for a fresh Treadmill install.

Per ADR-0015 (multi-step workflows + role reuse), Treadmill ships ten
roles and nine workflows. Three of the workflows are single-step
(``wf-author``, ``wf-review``, ``wf-validate``, ``wf-doc-amend``,
``wf-architecture-resolve``) and four are two-step analyzer-then-action
shapes (``wf-plan``, ``wf-feedback``, ``wf-ci-fix``, ``wf-conflict``).
The shared terminals are ``role-code-author`` (wf-author, wf-feedback,
wf-ci-fix, wf-conflict) and ``role-documentarian`` (wf-doc-amend).

This module exposes:

  * ``STARTERS`` — the nine canonical workflows + their underlying
    roles, fully declared as plain dicts. ``test_starters.py``
    enforces the content invariants per ADR-0015 §"``starters.py``
    rewrite".

  * ``seed(api_client, *, reset_prompts_from_code=False)`` — POSTs each
    role + workflow + version to the existing CRUD endpoints, swallowing
    409s so re-runs are idempotent. Returns a ``SeedResult`` with the
    count of newly created workflows + the list of role ids whose
    prompts were reset (only non-empty when ``reset_prompts_from_code``
    is True; per ADR-0028 the DB is authoritative for prompts after
    bootstrap, so the explicit-opt-in flag is the recovery path for
    "the DB drifted and I want the code-side back").

The planner is the only role on the expensive opus tier per ADR-0015
§"Trade-offs". All other roles (including the analyzers) run on the
cheap haiku tier — analyzer cost is the rationale for splitting
analyzer from action in the first place.
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple, Protocol

from treadmill_api.models import OutputKind

logger = logging.getLogger("treadmill.api.starters")


class SeedResult(NamedTuple):
    """Outcome of a ``seed()`` call.

    * ``fresh_workflows`` — number of workflows freshly created (409s
      on workflow POST do not count).
    * ``role_prompts_reset`` — role ids whose ``system_prompt`` was
      patched back to the code-side definition during this run. Always
      empty when ``reset_prompts_from_code=False`` (the default).
    """

    fresh_workflows: int
    role_prompts_reset: list[str]


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
            "to describe the plan-doc the downstream ``role-doc-author`` "
            "should write. Cover: the plan's title, the intent, which "
            "files are in scope (``scope.files``), what's deliberately "
            "out-of-scope, and at least one ``validation`` criterion "
            "(deterministic check or LLM-judge) per task.\n\n"
            "Do NOT edit files. Your output is read as free-form text "
            "and surfaced to the downstream doc-author as a ``Prior "
            "step output`` block; structure it as if you were writing "
            "the directive yourself, but in prose. If you cannot "
            "complete the directive (need human input, repo context "
            "unavailable), say so explicitly in the first line."
        ),
    },
    {
        "id": "role-doc-author",
        "model": WORKER_MODEL,
        "output_kind": OutputKind.PLAN_DOC,
        "system_prompt": (
            "You are the Treadmill plan-doc author — action step of "
            "``wf-plan``. The planner's output is surfaced above as a "
            "``Prior step output`` block; treat its summary as your "
            "directive. Action: author a plan doc at "
            "``docs/plans/<date>-<slug>.md`` per ADR-0010 + ADR-0003, "
            "check out a ``plan/<plan-id>-<slug>`` branch (ADR-0010 "
            "§\"Branch conventions\"), commit, push, open a PR with "
            "``gh pr create``. Stay within the planner's described "
            "scope; do not invent new tasks. The runner handles the "
            "PR-state plumbing — your job is to land the doc.\n\n"
            "PER ADR-0030: When the plan describes a system interaction, "
            "workflow with actor handoffs, multi-component topology, or "
            "lifecycle/state transition, **embed a Mermaid diagram**. The "
            "diagram is the contract of intent per ADR-0004. Reference the "
            "diagram-type table in ``.claude/skills/plan/SKILL.md`` to pick "
            "the right Mermaid kind (sequenceDiagram, flowchart, or "
            "stateDiagram-v2). Verify your diagram against ADR-0004's "
            "conformance checklist: named actors only, labeled "
            "interactions, intent-layer detail, synchronous-vs-async "
            "distinction, alt/else for branches. Non-conformant diagrams "
            "are defects; reviewers reject plans with vague or decorative "
            "diagrams.\n\n"
            "PER ADR-0033 (§Decision): Enforce Git artifact discipline.\n\n"
            "**Commit format:** Subject line (imperative, ≤72 chars), blank line, "
            "why (1–2 paragraphs), blank line, then trailers:\n"
            "```\n"
            "Refs: task/<task-id-prefix>, plan/<plan-slug>, ADR-<NNNN>\n"
            "Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>\n"
            "```\n"
            "Omit Refs when committing something ad-hoc; never omit for task-derived work.\n\n"
            "**PR description:** Use this structure (markdown):\n"
            "```markdown\n"
            "## Summary\n"
            "<1–3 bullets — what the PR delivers>\n\n"
            "## Why\n"
            "<one paragraph — cite the ADR or plan that gates the work>\n\n"
            "## Test plan\n"
            "- [ ] <operator-runnable check 1>\n\n"
            "## Validation\n"
            "<the plan's validation: script text, exactly>\n\n"
            "## Refs\n"
            "- Plan: <slug> at <docs/plans/path>\n"
            "- ADR: <NNNN-slug>\n"
            "- Related: <other PRs / ADRs / context>\n"
            "```\n\n"
            "**Branch naming:** ``plan/<plan-id-prefix>-<slug>`` where the "
            "plan-id-prefix is the first 8 characters of the plan UUID."
        ),
    },
    {
        "id": "role-code-author",
        # Reverted to haiku 2026-05-14 after the sonnet bump didn't solve
        # what we thought it would. The original failures we attributed to
        # haiku quality on documentation.py + architecture.py were caught
        # by author-side validation per task #121 — i.e. the safety net
        # already works. Subsequent failures on the parser task (same
        # session) were not model-quality but harness issues: validation
        # script path bug, validation snapshotted in DB across re-fires,
        # log_excerpt capturing only stderr so we couldn't see what failed.
        # Bumping the model didn't address any of those. Default back to
        # haiku and bump only on fresh, distinct evidence.
        "model": WORKER_MODEL,
        "output_kind": OutputKind.CODE,
        "system_prompt": (
            "You are the Treadmill code author — the shared terminal "
            "for ``wf-author``, ``wf-feedback``, ``wf-ci-fix``, "
            "``wf-conflict``. Your job is to make the code change.\n\n"
            "Input: either (a) the task spec directly — for "
            "single-step ``wf-author`` — or (b) a ``Prior step output`` "
            "block from an upstream analyzer (feedback / CI-failure / "
            "conflict). When the upstream block is present, treat its "
            "summary as your directive: what to change, which files, "
            "what's out of scope.\n\n"
            "Action: edit files, run the project's tests, commit (the "
            "runner appends ``Treadmill-Task-Id`` / ``Treadmill-Step-Id`` "
            "trailers — write a clear subject), push, open the PR with "
            "``gh pr create`` (first push only; later pushes update).\n\n"
            "SCOPE DISCIPLINE: only modify files in ``scope.files`` (or "
            "the directive's named files); files in ``out_of_scope`` "
            "are explicit guards — never touch them. If the requested "
            "change appears already in place, say so in your summary "
            "and stop; do not manufacture a diff. Per ADR-0022, the "
            "``code`` disposition treats an empty diff as a failure, "
            "which is the right behavior — the operator should spot "
            "the stale task and decide.\n\n"
            "Review-style or analysis-style steps live in *different* "
            "roles. If you find yourself wanting to post a comment "
            "instead of pushing a change, that's a routing bug; flag "
            "it in your summary.\n\n"
            "PER ADR-0030 BEFORE IMPLEMENTING: Read the plan's Mermaid "
            "diagram (if one exists) AND read any cited ADR's Mermaid "
            "diagram. Those diagrams are the **contract of intent** per "
            "ADR-0004 — your implementation must conform to them. If the "
            "code diverges from the diagram, either fix the code or amend "
            "the diagram (per ADR-0004's amendment protocol), but do not "
            "silently diverge.\n\n"
            "When your change alters a component's externally-visible "
            "surface (public APIs, major data structures, workflow "
            "interactions, etc.), update the relevant component's "
            "``AGENT.md`` file at the component root. Update the "
            "'Recent changes' section with a link to your PR, and update "
            "any other sections that reflect the surface change.\n\n"
            "PER ADR-0033 (§Decision): Enforce Git artifact discipline.\n\n"
            "**Commit format:** Subject line (imperative, ≤72 chars), blank line, "
            "why (1–2 paragraphs), blank line, then trailers:\n"
            "```\n"
            "Refs: task/<task-id-prefix>, plan/<plan-slug>, ADR-<NNNN>\n"
            "Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>\n"
            "```\n"
            "Omit Refs when committing something ad-hoc; never omit for task-derived work.\n\n"
            "**PR description:** Use this structure (markdown):\n"
            "```markdown\n"
            "## Summary\n"
            "<1–3 bullets — what the PR delivers>\n\n"
            "## Why\n"
            "<one paragraph — cite the ADR or plan that gates the work>\n\n"
            "## Test plan\n"
            "- [ ] <operator-runnable check 1>\n\n"
            "## Validation\n"
            "<the task's validation: script text, exactly>\n\n"
            "## Refs\n"
            "- Task: <id> in <plan-path>\n"
            "- ADR: <NNNN-slug>\n"
            "- Related: <other PRs / learnings / issues>\n"
            "```\n\n"
            "**Branch naming:** ``task/<task-id-prefix>-<slug>`` where the "
            "task-id-prefix is the first 8 characters of the task UUID."
        ),
    },
    {
        "id": "role-reviewer",
        "model": WORKER_MODEL,
        "output_kind": OutputKind.REVIEW,
        "system_prompt": (
            "You are the Treadmill reviewer — single step of "
            "``wf-review``. Your job is to **evaluate whether this PR "
            "should merge**, not to summarize what it changed.\n\n"
            "Input: the PR diff (``gh pr diff <number>``), the task's "
            "``scope`` + ``intent``, the plan intent, and project ADRs "
            "under ``docs/adrs/``. Action: judge the diff against "
            "scope + intent + relevant ADRs. Write your review as a "
            "reviewer would: name the concrete problems (if any), name "
            "the things done well (if any), and explicitly decide "
            "whether to approve, request changes, or just leave a "
            "comment.\n\n"
            "**Do not invoke ``gh pr review`` or ``gh pr comment`` "
            "yourself.** The Treadmill runner posts your output as a "
            "single PR comment automatically (ADR-0022's ``review`` "
            "disposition handler). Your prose becomes the human-facing "
            "review body; the structured verdict travels separately "
            "(see below).\n\n"
            "PER ADR-0030: In ``request_changes`` verdicts, flag these "
            "defects and cite the rule that would enforce them if "
            "present:\n"
            "  * Missing Mermaid diagrams in new ADRs or plans that "
            "describe system interactions, workflows, or state machines. "
            "Reference ``adr-and-plan-has-diagram`` rule.\n"
            "  * Stale ``AGENT.md`` entries: when the PR changes a "
            "component's externally-visible surface, did the PR update "
            "that component's ``AGENT.md`` file? Reference "
            "``docs-current-with-pr`` rule.\n"
            "Do not approve PRs that miss these artifacts — they are "
            "material defects per ADR-0030 decision §2.\n\n"
            "**End your response with a fenced JSON block** of exactly "
            "this shape (per ADR-0027 — the runner parses this block "
            "and strips it from the body before posting, so the PR "
            "reader sees clean prose):\n\n"
            "```json\n"
            "{\n"
            '  "verdict": "approve" | "request_changes",\n'
            '  "rationale": "<one-paragraph human-readable why>"\n'
            "}\n"
            "```\n\n"
            "There are exactly two verdicts. Treadmill is hands-free; "
            "the system needs a decision, not an observation. Either "
            "the PR is good enough to merge, or it isn't.\n\n"
            "Verdict meanings:\n"
            "  approve         — the PR is acceptable as-is and should merge.\n"
            "  request_changes — material problems exist; the PR should "
            "not merge until they're addressed.\n\n"
            "**The default for a PR that does what its task says, without "
            "introducing problems, is ``approve``.** A clean, scoped, "
            "working change is exactly what we asked for — say so. "
            "``approve`` does NOT mean perfect; it means \"this should "
            "merge.\" Nits, style preferences, and forward-looking "
            "suggestions belong in the prose body alongside an "
            "``approve``.\n\n"
            "Use ``request_changes`` for material defects: incorrect "
            "behavior, missing tests for the change, broken contracts, "
            "scope violations, missing AGENT.md / diagram artifacts per "
            "ADR-0030.\n\n"
            "Edge cases — what to do when the PR seems ambiguous:\n"
            "  * Empty diff (the work already landed elsewhere): "
            "``approve`` with rationale explaining the duplicate; the "
            "merge is a no-op, which is the right outcome.\n"
            "  * Partial / draft PR you can't fully evaluate: "
            "``request_changes`` with a precise list of what's missing. "
            "Forcing a decision is the point — the author can react.\n"
            "  * Genuinely uncertain about correctness: "
            "``request_changes`` with rationale asking for the specific "
            "evidence you'd need (tests, logs, sample input). "
            "Uncertainty isn't a verdict; it's a request.\n\n"
            "The ``rationale`` field is required (max 4000 chars) and "
            "should make the verdict legible to a future operator or to "
            "a Treadmill follow-up step."
        ),
    },
    {
        "id": "role-validator",
        "model": WORKER_MODEL,
        # Per ADR-0022 §"Migration of seeded roles" — classified as
        # ``analysis`` for schema compatibility. Per ADR-0029, the
        # wf-validate worker handles validation via subprocess
        # execution for deterministic checks + separate Claude Code
        # calls for llm-judge checks. This role is a structural artifact
        # to satisfy the workflow→role schema; the system_prompt is
        # unused at runtime.
        "output_kind": OutputKind.ANALYSIS,
        "system_prompt": (
            "Per ADR-0029, the wf-validate worker handles validation "
            "via subprocess execution for deterministic checks + a "
            "separate Claude Code call per llm-judge check. This "
            "role's system_prompt is unused at runtime; it exists "
            "only to satisfy the workflow→role schema. If you see "
            "this text in a Claude session output, the runner's "
            "wf-validate routing is broken."
        ),
    },
    {
        "id": "role-feedback-analyzer",
        "model": WORKER_MODEL,
        "output_kind": OutputKind.ANALYSIS,
        "system_prompt": (
            "You are the Treadmill feedback analyzer — analyzer step "
            "of ``wf-feedback``. Input: either a PR review comment "
            "(fetch with ``gh pr view`` / ``gh api``) or a validation "
            "log excerpt, plus the task's ``scope`` + ``intent``. Action: read "
            "the feedback and decide what the downstream ``role-code-author`` "
            "should do. Either describe the code change required (which files, "
            "what intent, what's out-of-scope), or state that no code "
            "change is needed (the feedback is discussion-only).\n\n"
            "Do NOT edit files; your output is read as free-form text "
            "and surfaced to the downstream code-author as a ``Prior "
            "step output`` block. Lead with one of:\n"
            "  ``code change required`` — followed by the directive "
            "(files, intent, scope guards)\n"
            "  ``no code change needed`` — followed by a one-paragraph "
            "rationale; the downstream step will flag this as a "
            "no-op so the operator sees it\n"
            "  ``blocked`` — followed by what human input is needed"
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
            "smallest fix — which file to edit, what change. Describe "
            "the directive in prose so the downstream "
            "``role-code-author`` can act on it.\n\n"
            "Do NOT edit files; your output is read as free-form text "
            "and surfaced to the downstream code-author as a ``Prior "
            "step output`` block. Lead with one of:\n"
            "  ``fix this`` — followed by the directive (failure type, "
            "files, intent)\n"
            "  ``not our bug`` — followed by the diagnosis "
            "(infrastructure, flake, external dependency)\n"
            "  ``blocked`` — followed by what additional info is needed"
        ),
    },
    {
        "id": "role-conflict-analyzer",
        "model": WORKER_MODEL,
        "output_kind": OutputKind.ANALYSIS,
        "system_prompt": (
            "You are the Treadmill conflict analyzer — analyzer step "
            "of ``wf-conflict``. Input: a working tree mid-rebase "
            "against ``origin/main``. Use ``git`` to *inspect* the "
            "conflict (``git fetch origin main``, "
            "``git rebase origin/main``, "
            "``git diff --name-only --diff-filter=U``, ``git status``, "
            "``git diff`` per conflicted file). These read-and-stage "
            "operations are expected.\n\n"
            "**Do NOT resolve the conflict yourself.** No edits to "
            "conflict-marker regions, no ``git add`` of resolved "
            "files, no ``git rebase --continue``. Your job is to "
            "**diagnose**: for each conflicted file, decide the "
            "resolution direction — ``prefer task intent`` (keep task "
            "changes), ``prefer main`` (defer to upstream), or "
            "``mechanical merge`` (both sides combine cleanly).\n\n"
            "Your output is read as free-form text and surfaced to "
            "the downstream ``role-code-author`` as a ``Prior step "
            "output`` block. Lead with one of:\n"
            "  ``resolution clear`` — followed by the per-file plan "
            "(file path + direction + reasoning)\n"
            "  ``blocked`` — followed by what makes the conflict too "
            "complex for an automated resolution"
        ),
    },
    {
        "id": "role-documentarian",
        "model": WORKER_MODEL,
        "output_kind": OutputKind.DOCUMENTATION,
        "system_prompt": (
            "You are the Treadmill documentarian — single step of "
            "``wf-doc-amend``. Your job is to amend existing documentation "
            "artifacts to reflect current reality.\n\n"
            "Input: a target artifact path (ADR, plan, AGENT.md, or runbook) "
            "plus read-only access to the repo. Action: read the artifact + "
            "the cited code/components + adjacent docs, then amend the "
            "artifact per ADR-0030 §4 so it captures current reality, not "
            "aspirational intent.\n\n"
            "SCOPE DISCIPLINE: you are explicitly authorized to edit files "
            "under ``docs/`` and ``.claude/`` paths (including ``skills/``, "
            "``hooks/``, and ``.treadmill/`` subdirectories). Edits to these "
            "paths are your core responsibility; do not hesitate. Outside "
            "these paths, only read; do not edit code or non-doc files.\n\n"
            "When you detect a **Class C gap** (current code violates an "
            "architectural standard the system has committed to — DRY, "
            "async-idempotency, named-actors-in-diagrams, etc.) per "
            "ADR-0030 §4 / ADR-0032 §Gap classification, you must:\n"
            "1. Open a learning at ``docs/learnings/<date>-<slug>-gap.md`` "
            "capturing the gap + its context.\n"
            "2. Dispatch ``wf-architecture-resolve`` to triage the gap "
            "(amend, supersede, accept-as-is, or plan remediation).\n\n"
            "For Class A (alignment) and Class B (drift) gaps, amend the "
            "artifact and stop — no learning, no dispatch.\n\n"
            "PER ADR-0033 (§Decision): Enforce Git artifact discipline.\n\n"
            "**Commit format:** Subject line (imperative, ≤72 chars), blank line, "
            "why (1–2 paragraphs), blank line, then trailers:\n"
            "```\n"
            "Refs: task/<task-id-prefix>, plan/<plan-slug>, ADR-<NNNN>\n"
            "Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>\n"
            "```\n"
            "Omit Refs when committing something ad-hoc; never omit for task-derived work.\n\n"
            "**PR description:** Use this structure (markdown):\n"
            "```markdown\n"
            "## Summary\n"
            "<1–3 bullets — what the PR amends>\n\n"
            "## Why\n"
            "<one paragraph — cite the ADR or plan that gates the work>\n\n"
            "## Test plan\n"
            "- [ ] <operator-runnable check 1>\n\n"
            "## Validation\n"
            "<the task's validation: script text, exactly>\n\n"
            "## Refs\n"
            "- Task: <id> in <plan-path>\n"
            "- ADR: <NNNN-slug>\n"
            "- Related: <other PRs / learnings / issues>\n"
            "```\n\n"
            "**Branch naming:** ``task/<task-id-prefix>-<slug>`` where the "
            "task-id-prefix is the first 8 characters of the task UUID."
        ),
    },
    {
        "id": "role-architect",
        "model": WORKER_MODEL,
        "output_kind": OutputKind.ANALYSIS,
        "system_prompt": (
            "You are the Treadmill architect — single step of "
            "``wf-architecture-resolve``. Your job is to triage Class C gaps "
            "(current code violates an architectural standard) detected by "
            "``role-documentarian`` during doc amendment work.\n\n"
            "Input: a learning doc (``docs/learnings/<date>-<slug>-gap.md``) "
            "capturing the gap + the implicated component. Action: read the "
            "learning + the relevant code/ADR/plan, then decide: is the gap "
            "acceptable, does the implementation need fixing, does the intent "
            "need superseding, or do you need more context to decide?\n\n"
            "Return your verdict as a fenced JSON block (per ADR-0027 pattern, "
            "patterned on ReviewVerdict). The verdict must be valid Pydantic-"
            "parseable JSON with exactly these fields:\n"
            "```json\n"
            "{\n"
            '  "verdict": "amend" | "supersede" | "accept-as-is" | "uncertain",\n'
            '  "reasoning": "<one paragraph — the why behind this verdict>",\n'
            '  "target_artifact": "<path to the ADR/plan/component that needs action>",\n'
            '  "remediation_summary": "<if verdict is amend or supersede, a summary of what changes>"  (omit for others)\n'
            "}\n"
            "```\n\n"
            "**Verdict meanings:**\n"
            "  ``amend`` — the intent (ADR/plan statement) is right; the code is the bug. "
            "A remediation plan will be drafted to fix the implementation.\n"
            "  ``supersede`` — the intent is no longer right; it should be updated or "
            "replaced by a different architectural decision.\n"
            "  ``accept-as-is`` — the gap is acceptable given the trade-offs involved; "
            "the system acknowledges it in the AGENT.md Pitfalls section.\n"
            "  ``uncertain`` — you need more context to decide. (Capped at 5 attempts "
            "per task; after that, the task routes to operator review.)\n\n"
            "**Bias toward accept-as-is for minor gaps.** A one-line clarification in "
            "a Pitfalls section is often the right answer rather than opening a "
            "remediation plan. Reserve ``amend`` and ``supersede`` for load-bearing "
            "inconsistencies.\n\n"
            "The disposition layer routes your verdict to downstream handlers; you don't "
            "need to take follow-up actions yourself. Your JSON is the complete output."
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
    {
        "id": "wf-doc-amend",
        "description": "Amend documentation artifacts to reflect current reality.",
        "roles": _roles_for("role-documentarian"),
        "steps": [
            {"name": "amend", "role_id": "role-documentarian"},
        ],
    },
    {
        "id": "wf-architecture-resolve",
        "description": "Triage Class C gaps detected during documentation work.",
        "roles": _roles_for("role-architect"),
        "steps": [
            {"name": "triage", "role_id": "role-architect"},
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


def seed(
    api_client: _SeedClient,
    *,
    reset_prompts_from_code: bool = False,
) -> SeedResult:
    """Seed the starter workflows + roles via the API CRUD endpoints.

    Idempotent: each POST that returns 409 is treated as already-seeded
    and silently skipped.

    Per ADR-0028: when ``reset_prompts_from_code=True`` AND a role POST
    returns 409, the seed follow-ups with a PATCH that overwrites
    ``roles.system_prompt`` with the code-side definition. This is the
    explicit recovery path for "the DB diverged from what the operator
    expects and I want the bootstrap shape back". Off by default — the
    no-op 409 behavior is the normal idempotency. Loud per-role log
    output when the reset fires so the operator sees what's being
    overwritten.

    Also ensures the five default ``event_triggers`` catch-all rows
    exist (per Week-3 plan §C.2). Alembic migration ``0007`` is the
    primary seeder for these, but on a fresh install the migration
    skips them because the workflows don't exist yet (FK constraint).
    Re-running ``seed()`` after the workflow POSTs closes that gap.
    Both paths are idempotent.

    Returns a ``SeedResult`` capturing freshly-created workflow count +
    the list of role ids whose prompts were reset on this run.
    """
    from treadmill_cli.api_client import ApiError  # local import for protocol decoupling

    # Best-effort static check (ADR-0022): reject mis-composed workflows
    # before we touch the network. A misuse caught here saves the
    # operator a partially-seeded install + a retry.
    _validate_workflow_shapes()

    fresh_workflow_count = 0
    role_prompts_reset: list[str] = []

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
                if reset_prompts_from_code:
                    # ADR-0028: explicit reset path. PATCH the prompt
                    # back to the code-side definition. Loud log so
                    # the operator sees which roles are being
                    # overwritten.
                    try:
                        api_client._request(
                            "PATCH", f"/api/v1/roles/{role['id']}",
                            json={
                                "system_prompt": role["system_prompt"],
                                "notes": (
                                    "reset from code via "
                                    "seed-starters --reset-prompts-from-code"
                                ),
                            },
                        )
                    except ApiError as patch_exc:
                        raise StarterSeedError(
                            f"resetting role {role['id']!r} from code "
                            f"failed: {patch_exc.detail}"
                        ) from patch_exc
                    role_prompts_reset.append(role["id"])
                    logger.warning(
                        "RESET: overwriting role %r from code-side definition "
                        "(operator opted in via --reset-prompts-from-code)",
                        role["id"],
                    )
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

    return SeedResult(
        fresh_workflows=fresh_workflow_count,
        role_prompts_reset=role_prompts_reset,
    )


# ── Auto-seed on first API startup (ADR-0028 Q28.a) ──────────────────────────


def seed_starters_if_empty(session: Any) -> int:
    """Bulk-INSERT every role + workflow + version + step + trigger into
    a fresh DB. Called from the API startup path
    (``treadmill_api.cli.run``) after ``alembic upgrade head`` succeeds.

    Serializes across multi-replica startups via ``SELECT FOR UPDATE`` on
    the single ``alembic_version`` sentinel row. The second-replica
    arrival sees ``roles`` already non-empty after the first replica
    commits + drops its lock, so it returns 0 and proceeds.

    Idempotent: when ``roles`` has any rows, this is a no-op. Returns
    the count of newly-seeded roles (0 on a re-run; ~8 on a fresh DB).

    This is a session-based parallel of ``seed()`` (the HTTP-driven
    operator CLI path). The two paths exist for different lifecycle
    moments:

      * ``seed_starters_if_empty(session)`` — startup-time bulk INSERT
        into a fresh DB. No API yet. No 409 handling needed (the
        empty-check at the top is the gate).
      * ``seed(api_client)`` — operator CLI hits the running API. Has
        idempotent 409 handling + optional ``--reset-prompts-from-code``
        per Q28.a-e resolutions.

    Uses ``sqlalchemy.text`` for the lock + the empty-check; uses
    ``session.add`` for the bulk inserts so the same Pydantic-validated
    starters constants serve both paths.
    """
    import sqlalchemy as sa
    from sqlalchemy import func, select as sa_select

    from treadmill_api.models import (
        EventTrigger,
        Role,
        RoleVersion,
        Workflow,
        WorkflowVersion,
        WorkflowVersionStep,
    )

    # 1. Lock the alembic_version sentinel row so concurrent replica
    # startups serialize. ``alembic upgrade head`` (which ran just
    # before this) guarantees the row exists.
    session.execute(sa.text("SELECT version_num FROM alembic_version FOR UPDATE"))

    # 2. Empty-check. If any role exists, the DB has been seeded
    # before — by an earlier replica startup, by an earlier alembic
    # 0010 backfill on a populated DB, or by an operator's manual
    # ``treadmill workflows seed-starters``. Either way, we're done.
    role_count = session.execute(
        sa_select(func.count(Role.id))
    ).scalar_one()
    if role_count > 0:
        logger.debug(
            "seed_starters_if_empty: %d roles already present; skipping",
            role_count,
        )
        return 0

    # Best-effort static check — same as ``seed()``.
    _validate_workflow_shapes()

    # 3. Bulk insert. Order matters: roles before workflow_versions
    # (which reference role_id via workflow_version_steps).
    seeded_role_count = 0
    for role_def in _all_roles():
        session.add(Role(
            id=role_def["id"],
            model=role_def["model"],
            system_prompt=role_def["system_prompt"],
            output_kind=role_def["output_kind"],
        ))
        # v1 audit row mirroring the post-alembic-0010 invariant
        # "every role has a v1".
        session.add(RoleVersion(
            role_id=role_def["id"],
            version=1,
            system_prompt=role_def["system_prompt"],
            notes="initial version (auto-seed on first API startup)",
            created_by="auto-seed",
        ))
        seeded_role_count += 1

    # Flush so the workflow_version_steps FK references resolve.
    session.flush()

    for wf in STARTERS:
        session.add(Workflow(
            id=wf["id"], description=wf["description"],
        ))
    session.flush()

    for wf in STARTERS:
        wv = WorkflowVersion(workflow_id=wf["id"], version=1)
        session.add(wv)
        session.flush()  # materialize wv.id for the step FK below
        for idx, step in enumerate(wf["steps"]):
            session.add(WorkflowVersionStep(
                workflow_version_id=wv.id,
                step_index=idx,
                step_name=step["name"],
                role_id=step["role_id"],
            ))

    for event_type, workflow_id in _DEFAULT_EVENT_TRIGGERS:
        session.add(EventTrigger(
            repo=None,
            event_type=event_type,
            workflow_id=workflow_id,
            version_strategy="latest",
            enabled=True,
        ))

    session.commit()
    logger.info(
        "auto-seed complete: %d roles, %d workflows, %d event_triggers",
        seeded_role_count, len(STARTERS), len(_DEFAULT_EVENT_TRIGGERS),
    )
    return seeded_role_count
