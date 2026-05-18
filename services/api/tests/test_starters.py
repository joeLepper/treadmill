"""Content tests for the starter-workflow seed module.

Per ADR-0015 §"``starters.py`` rewrite" and ADR-0032, every install ships with the
canonical ten roles + nine workflows. Five workflows are single-step;
four (``wf-plan``, ``wf-feedback``, ``wf-ci-fix``, ``wf-conflict``) are
two-step analyzer-then-action shapes. These tests enforce the content
invariants of the static ``STARTERS`` list — every step references a
defined role, no slug appears twice, two-step shapes follow the analyzer-
then-action pattern, ``role-code-author`` is reused by exactly four workflows.
The integration test (``cli/tests/test_integration_cli_seed.py``) exercises
seeding against a live API.
"""

from __future__ import annotations

import pytest

from treadmill_api.models import OutputKind
from treadmill_api.starters import (
    PLANNER_MODEL,
    STARTERS,
    WORKER_MODEL,
    WorkflowShapeError,
    _all_roles,
    _validate_workflow_shapes,
)


# Expected workflow id-set + role id-set per ADR-0015 §"Role taxonomy"
# and §"Per-workflow shape matrix", plus ADR-0032 (documentarian + architect).
# Kept as module-level constants so the assertions below read declaratively.

_EXPECTED_WORKFLOW_IDS = {
    "wf-author",
    "wf-plan",
    "wf-review",
    "wf-validate",
    "wf-feedback",
    "wf-ci-fix",
    "wf-conflict",
    "wf-doc-amend",
    "wf-architecture-resolve",
    "wf-crystallize-learning",
    "wf-audit-rule-corpus",
}

_EXPECTED_ROLE_IDS = {
    "role-planner",
    "role-doc-author",
    "role-code-author",
    "role-reviewer",
    "role-validator",
    "role-feedback-analyzer",
    "role-ci-analyzer",
    "role-conflict-analyzer",
    "role-documentarian",
    "role-architect",
    "role-crystallization-judge",
    "role-rule-corpus-auditor",
}

# Action-role ids that may appear as step 2 of a 2-step workflow. Per
# ADR-0015, the analyzer-then-action shape always terminates in one
# of these. Per ADR-0034, ``role-architect`` is the step-2 action for
# ``wf-crystallize-learning`` — it authors the rule YAML + check.sh
# when the judge verdict is ``ready``.
_ACTION_ROLE_IDS = {"role-code-author", "role-doc-author", "role-architect"}


# ── Coverage ─────────────────────────────────────────────────────────────────


def test_starters_has_eleven_canonical_workflows() -> None:
    """The canonical workflows, exactly (per ADR-0015 + ADR-0032 +
    ADR-0034). Future starters extend this list; the count is asserted
    as a tripwire so additions are intentional."""
    ids = {wf["id"] for wf in STARTERS}
    assert ids == _EXPECTED_WORKFLOW_IDS


def test_starters_declares_twelve_canonical_roles() -> None:
    """Per ADR-0015 §"Role taxonomy" + ADR-0032 + ADR-0034 — twelve
    roles, exactly."""
    ids = {role["id"] for role in _all_roles()}
    assert ids == _EXPECTED_ROLE_IDS


# ── Shape ────────────────────────────────────────────────────────────────────


def test_every_starter_has_required_fields() -> None:
    """Each starter dict has id, description, roles, steps. Required by
    the seed() function's POSTs."""
    for wf in STARTERS:
        assert wf["id"], f"starter missing id: {wf}"
        assert wf["description"], f"starter {wf['id']!r} missing description"
        assert wf["roles"], f"starter {wf['id']!r} has no roles declared"
        assert wf["steps"], f"starter {wf['id']!r} has no steps declared"


def test_every_step_role_resolves_to_a_declared_role() -> None:
    """A step's role_id must reference a role defined in the same workflow's
    roles list. The seed function POSTs roles before workflows, so an
    unresolved reference would 400 on POST /api/v1/workflows/{id}/versions."""
    for wf in STARTERS:
        declared_role_ids = {r["id"] for r in wf["roles"]}
        for step in wf["steps"]:
            assert step["role_id"] in declared_role_ids, (
                f"starter {wf['id']!r} step {step['name']!r} references "
                f"undefined role {step['role_id']!r}; declared: {declared_role_ids}"
            )


def test_every_step_role_is_defined_in_the_global_roles_list() -> None:
    """Beyond the per-workflow ``roles`` declaration, every step's
    role_id must also resolve to a role in the global roles list
    (``_all_roles()``). This is what gets POSTed to /api/v1/roles."""
    defined_ids = {role["id"] for role in _all_roles()}
    for wf in STARTERS:
        for step in wf["steps"]:
            assert step["role_id"] in defined_ids, (
                f"step {wf['id']!r}.{step['name']!r} references "
                f"role {step['role_id']!r} which is not defined globally; "
                f"defined: {sorted(defined_ids)}"
            )


# ── Uniqueness ───────────────────────────────────────────────────────────────


def test_no_duplicate_workflow_ids() -> None:
    ids = [wf["id"] for wf in STARTERS]
    assert len(ids) == len(set(ids)), f"duplicate workflow ids: {ids}"


def test_no_duplicate_role_ids() -> None:
    """The same role may be referenced by multiple workflows (and
    ``role-code-author`` very much is — see the four-workflow invariant
    below) but its definition is single-source-of-truth in ``_ROLES``.
    Asserting via ``_all_roles()`` confirms the dedup is honest."""
    ids = [role["id"] for role in _all_roles()]
    assert len(ids) == len(set(ids)), f"duplicate role ids: {ids}"


# ── Required role fields ─────────────────────────────────────────────────────


def test_every_role_has_required_fields() -> None:
    """Per ADR-0015 — every role carries id, model, system_prompt. The
    CRUD endpoints require all three; an empty value would 422 the seed."""
    for role in _all_roles():
        assert role.get("id"), f"role missing id: {role}"
        assert role.get("model"), f"role {role['id']!r} missing model"
        assert role.get("system_prompt"), (
            f"role {role['id']!r} missing system_prompt"
        )


# Per ADR-0022 §"Migration of seeded roles" + ADR-0032 — the canonical mapping of
# each seeded role to its declared output kind. The runner's dispatch
# table reads this field; a drift here means the runner can't pick the
# right disposition handler at run time.
_EXPECTED_OUTPUT_KINDS: dict[str, OutputKind] = {
    "role-code-author": OutputKind.CODE,
    "role-doc-author": OutputKind.PLAN_DOC,
    "role-planner": OutputKind.ANALYSIS,
    "role-reviewer": OutputKind.REVIEW,
    "role-validator": OutputKind.ANALYSIS,  # structural artifact per ADR-0029
    "role-feedback-analyzer": OutputKind.ANALYSIS,
    "role-ci-analyzer": OutputKind.ANALYSIS,
    "role-conflict-analyzer": OutputKind.ANALYSIS,
    "role-documentarian": OutputKind.DOCUMENTATION,
    "role-architect": OutputKind.ANALYSIS,
    "role-crystallization-judge": OutputKind.ANALYSIS,
    "role-rule-corpus-auditor": OutputKind.ANALYSIS,
}


def test_every_seeded_role_declares_output_kind() -> None:
    """ADR-0022 — every seeded role must declare its output kind so
    the runner's per-kind dispatch table can route it. Missing the
    field would mean the worker raises ``UnknownOutputKindError`` at
    run time."""
    for role in _all_roles():
        assert "output_kind" in role, (
            f"role {role['id']!r} is missing output_kind; per ADR-0022 "
            "every seeded role must declare its kind"
        )
        assert isinstance(role["output_kind"], OutputKind), (
            f"role {role['id']!r} output_kind must be an OutputKind "
            f"enum value, got {type(role['output_kind']).__name__}"
        )


def test_seeded_roles_output_kinds_match_adr_0022() -> None:
    """Each seeded role's output_kind matches the ADR-0022 mapping.

    The mapping is the contract between the role's declared intent
    (its system prompt) and the runner's behavior (the dispatch
    handler picked at run time). Drift here is a load-bearing bug:
    a role classified as ``code`` but prompted to do a review would
    fail with ``CodeAuthorError`` on every run.
    """
    by_id = {r["id"]: r for r in _all_roles()}
    for role_id, expected in _EXPECTED_OUTPUT_KINDS.items():
        actual = by_id[role_id]["output_kind"]
        assert actual is expected, (
            f"role {role_id!r} declares output_kind={actual!r}, "
            f"ADR-0022 mapping says {expected!r}"
        )


def test_role_model_tier_invariant() -> None:
    """Cost / capability discipline — each role is on the right model tier:

    * ``role-planner``     → opus (deliberative; expensive)
    * ``role-architect``   → sonnet (rarely-dispatched arbiter; cost is
      not a concern, structured-output reliability is — bumped 2026-05-15
      after haiku failed to emit the required JSON envelope on a
      deadlock arbitration)
    * everyone else        → haiku (analyzers, reviewer, validator,
      code-author, doc-author, documentarian — cheap tier; rules
      override per ADR-0029 Q29.b when an llm-judge needs more
      capability. role-code-author was briefly on sonnet 2026-05-14 but
      reverted same-day after the bump didn't address the actual failure
      modes, which turned out to be harness gaps not model quality.)
    """
    SONNET_MODEL = "claude-sonnet-4-6"
    SONNET_ROLES = {"role-architect"}
    roles_by_id = {r["id"]: r for r in _all_roles()}
    assert roles_by_id["role-planner"]["model"] == PLANNER_MODEL, (
        f"role-planner must use {PLANNER_MODEL!r}"
    )
    for role_id, role in roles_by_id.items():
        if role_id == "role-planner":
            continue
        if role_id in SONNET_ROLES:
            assert role["model"] == SONNET_MODEL, (
                f"role {role_id!r} should be on {SONNET_MODEL!r}, "
                f"got {role['model']!r}"
            )
            continue
        assert role["model"] == WORKER_MODEL, (
            f"role {role_id!r} should be on {WORKER_MODEL!r}, got {role['model']!r}"
        )


def test_all_roles_have_substantive_system_prompts() -> None:
    """Every role's system_prompt is at least a one-paragraph posture
    statement. The placeholder bound is ~100 chars; the C.3 follow-up
    elaborates each to the full role prompt. Below 50 chars is a
    smoke test — clearly broken if a role's prompt is shorter than a
    tweet."""
    for role in _all_roles():
        assert role["system_prompt"], f"role {role['id']!r} has empty prompt"
        assert len(role["system_prompt"]) >= 50, (
            f"role {role['id']!r} prompt is too short to be useful "
            f"({len(role['system_prompt'])} chars)"
        )


# ── C.3: full-prompt content invariants ─────────────────────────────────────


# The per-workflow decision values per ADR-0012 §"Decision-string
# value-sets per workflow". Every role's prompt must list at least one
# of these for the workflow(s) it serves.
_WORKFLOW_DECISION_VALUES: dict[str, set[str]] = {
    "wf-author": {"pushed", "blocked", "no-changes"},
    "wf-plan-analyzer": {"plan-ready", "blocked"},
    "wf-plan-action": {"plan-doc-pushed", "blocked"},
    "wf-review": {"approved", "changes_requested", "needs-more-info"},
    "wf-validate": {"pass", "fail", "error"},
    "wf-feedback-analyzer": {"plan-ready", "no-action-needed", "blocked"},
    "wf-feedback-action": {
        "code-change-dispatched", "responded-without-change", "blocked",
    },
    "wf-ci-fix-analyzer": {"plan-ready", "not-our-bug", "blocked"},
    "wf-ci-fix-action": {"fix-pushed", "gave-up", "not-our-bug"},
    "wf-conflict-analyzer": {"plan-ready", "blocked"},
    "wf-conflict-action": {"resolved", "gave-up"},
}

# Per-role decision-value buckets the prompt must reference. The
# ``role-code-author`` is the shared terminal across four workflows,
# so its prompt must reference values from all four.
_ROLE_DECISION_BUCKETS: dict[str, list[str]] = {
    "role-planner": ["wf-plan-analyzer"],
    "role-doc-author": ["wf-plan-action"],
    "role-code-author": [
        "wf-author",
        "wf-feedback-action",
        "wf-ci-fix-action",
        "wf-conflict-action",
    ],
    "role-reviewer": ["wf-review"],
    "role-validator": ["wf-validate"],
    "role-feedback-analyzer": ["wf-feedback-analyzer"],
    "role-ci-analyzer": ["wf-ci-fix-analyzer"],
    "role-conflict-analyzer": ["wf-conflict-analyzer"],
}

# Analyzer roles per ADR-0015. Each must reference ``task_directive``
# in its prompt — that's the analyzer→action contract.
_ANALYZER_ROLE_IDS = {
    "role-planner",
    "role-feedback-analyzer",
    "role-ci-analyzer",
    "role-conflict-analyzer",
}


def test_role_reviewer_prompt_teaches_json_envelope() -> None:
    """Per ADR-0027 (resolved 2026-05-13), the review disposition
    handler parses a fenced JSON block as its primary verdict channel.
    The role-reviewer prompt must teach Claude the JSON envelope
    convention; without it, Claude defaults to free-form prose and
    the runner falls back to the regex tourniquet or to the safe
    default ``comment``.

    The prompt must reference ``json`` (case-insensitive) at least
    once — the runner expects a ```json fenced block. It must NOT
    reference the legacy ``VERDICT:`` marker (which lives now only
    in the runner-side tourniquet, not in the prompt).
    """
    reviewer = next(r for r in _all_roles() if r["id"] == "role-reviewer")
    prompt = reviewer["system_prompt"]
    assert "json" in prompt.lower(), (
        "role-reviewer prompt must teach the JSON envelope (ADR-0027). "
        "The runner parses the last ``json fenced block to extract "
        "verdict + rationale."
    )
    assert "VERDICT:" not in prompt, (
        "ADR-0027 phase 3 dropped the prose VERDICT: marker from the "
        "prompt. The runner's tourniquet regex still parses it as a "
        "fallback, but the prompt should no longer teach it; mixing "
        "the two conventions confuses the model."
    )
    for value in ("approve", "request_changes", "comment"):
        assert value in prompt, (
            f"role-reviewer prompt must reference verdict value {value!r}; "
            "the runner won't recognize verdicts the prompt doesn't teach."
        )
    # The fenced JSON example should appear in the prompt so the
    # model has a literal template to mirror.
    assert "```json" in prompt, (
        "role-reviewer prompt should include a literal ```json fence "
        "example so the model has a concrete template."
    )


def test_role_code_author_prompt_mentions_scope_discipline() -> None:
    """The shared code-author terminal runs across four workflows; its
    prompt must name the scope invariant so the role doesn't drift
    outside its directive's files."""
    code_author = next(r for r in _all_roles() if r["id"] == "role-code-author")
    prompt = code_author["system_prompt"]
    assert "scope" in prompt.lower(), (
        "role-code-author prompt must reference ``scope`` (the "
        "scope-discipline invariant per ADR-0015)"
    )


def test_role_code_author_prompt_teaches_adr_0033_commit_format() -> None:
    """Per ADR-0033 §Decision, role-code-author must teach the standardized
    commit message format: subject ≤72, why, Refs: and Co-Authored-By
    trailers."""
    code_author = next(r for r in _all_roles() if r["id"] == "role-code-author")
    prompt = code_author["system_prompt"]
    assert "ADR-0033" in prompt, (
        "role-code-author prompt must reference ADR-0033 for commit discipline"
    )
    assert "Refs:" in prompt and "Co-Authored-By:" in prompt, (
        "role-code-author prompt must teach Refs: and Co-Authored-By: trailers"
    )
    assert "≤72" in prompt or "72 chars" in prompt, (
        "role-code-author prompt must specify subject line ≤72 chars"
    )


def test_role_code_author_prompt_teaches_adr_0033_pr_format() -> None:
    """Per ADR-0033 §Decision, role-code-author must teach the standardized
    PR description structure: Summary / Why / Test plan / Validation / Refs."""
    code_author = next(r for r in _all_roles() if r["id"] == "role-code-author")
    prompt = code_author["system_prompt"]
    required_sections = ["## Summary", "## Why", "## Test plan", "## Validation", "## Refs"]
    for section in required_sections:
        assert section in prompt, (
            f"role-code-author prompt must include {section!r} in PR description template"
        )


def test_role_code_author_prompt_teaches_adr_0033_branch_naming() -> None:
    """Per ADR-0033 §Decision, role-code-author must teach the standardized
    branch naming convention: task/<task-id-prefix>-<slug> with 8-char UUID."""
    code_author = next(r for r in _all_roles() if r["id"] == "role-code-author")
    prompt = code_author["system_prompt"]
    assert "task/" in prompt and "task-id-prefix" in prompt, (
        "role-code-author prompt must teach task/<task-id-prefix>-<slug> "
        "branch naming per ADR-0033"
    )
    assert "8" in prompt, (
        "role-code-author prompt must specify 8-char UUID prefix length"
    )


def test_role_doc_author_prompt_teaches_adr_0033_commit_format() -> None:
    """Per ADR-0033 §Decision, role-doc-author must teach the standardized
    commit message format: subject ≤72, why, Refs: and Co-Authored-By
    trailers."""
    doc_author = next(r for r in _all_roles() if r["id"] == "role-doc-author")
    prompt = doc_author["system_prompt"]
    assert "ADR-0033" in prompt, (
        "role-doc-author prompt must reference ADR-0033 for commit discipline"
    )
    assert "Refs:" in prompt and "Co-Authored-By:" in prompt, (
        "role-doc-author prompt must teach Refs: and Co-Authored-By: trailers"
    )
    assert "≤72" in prompt or "72 chars" in prompt, (
        "role-doc-author prompt must specify subject line ≤72 chars"
    )


def test_role_doc_author_prompt_teaches_adr_0033_pr_format() -> None:
    """Per ADR-0033 §Decision, role-doc-author must teach the standardized
    PR description structure: Summary / Why / Test plan / Validation / Refs."""
    doc_author = next(r for r in _all_roles() if r["id"] == "role-doc-author")
    prompt = doc_author["system_prompt"]
    required_sections = ["## Summary", "## Why", "## Test plan", "## Validation", "## Refs"]
    for section in required_sections:
        assert section in prompt, (
            f"role-doc-author prompt must include {section!r} in PR description template"
        )


def test_role_doc_author_prompt_teaches_adr_0033_branch_naming() -> None:
    """Per ADR-0033 §Decision, role-doc-author must teach the standardized
    branch naming convention: plan/<plan-id-prefix>-<slug> with 8-char UUID."""
    doc_author = next(r for r in _all_roles() if r["id"] == "role-doc-author")
    prompt = doc_author["system_prompt"]
    assert "plan/" in prompt and "plan-id-prefix" in prompt, (
        "role-doc-author prompt must teach plan/<plan-id-prefix>-<slug> "
        "branch naming per ADR-0033"
    )
    assert "8" in prompt, (
        "role-doc-author prompt must specify 8-char UUID prefix length"
    )


def test_role_documentarian_prompt_teaches_adr_0033_commit_format() -> None:
    """Per ADR-0033 §Decision, role-documentarian must teach the standardized
    commit message format: subject ≤72, why, Refs: and Co-Authored-By
    trailers."""
    documentarian = next(r for r in _all_roles() if r["id"] == "role-documentarian")
    prompt = documentarian["system_prompt"]
    assert "ADR-0033" in prompt, (
        "role-documentarian prompt must reference ADR-0033 for commit discipline"
    )
    assert "Refs:" in prompt and "Co-Authored-By:" in prompt, (
        "role-documentarian prompt must teach Refs: and Co-Authored-By: trailers"
    )
    assert "≤72" in prompt or "72 chars" in prompt, (
        "role-documentarian prompt must specify subject line ≤72 chars"
    )


def test_role_documentarian_prompt_teaches_adr_0033_pr_format() -> None:
    """Per ADR-0033 §Decision, role-documentarian must teach the standardized
    PR description structure: Summary / Why / Test plan / Validation / Refs."""
    documentarian = next(r for r in _all_roles() if r["id"] == "role-documentarian")
    prompt = documentarian["system_prompt"]
    required_sections = ["## Summary", "## Why", "## Test plan", "## Validation", "## Refs"]
    for section in required_sections:
        assert section in prompt, (
            f"role-documentarian prompt must include {section!r} in PR description template"
        )


def test_role_documentarian_prompt_teaches_adr_0033_branch_naming() -> None:
    """Per ADR-0033 §Decision, role-documentarian must teach the standardized
    branch naming convention: task/<task-id-prefix>-<slug> with 8-char UUID."""
    documentarian = next(r for r in _all_roles() if r["id"] == "role-documentarian")
    prompt = documentarian["system_prompt"]
    assert "task/" in prompt and "task-id-prefix" in prompt, (
        "role-documentarian prompt must teach task/<task-id-prefix>-<slug> "
        "branch naming per ADR-0033"
    )
    assert "8" in prompt, (
        "role-documentarian prompt must specify 8-char UUID prefix length"
    )


def test_role_architect_prompt_teaches_json_envelope() -> None:
    """Per ADR-0032 Q32.d, the architect role must return a Pydantic-validated
    ArchitectVerdict JSON envelope, patterned on ADR-0027's ReviewVerdict.
    The prompt must teach the JSON envelope convention with the four verdict
    values: amend / supersede / accept-as-is / uncertain."""
    architect = next(r for r in _all_roles() if r["id"] == "role-architect")
    prompt = architect["system_prompt"]
    assert "json" in prompt.lower(), (
        "role-architect prompt must teach the JSON envelope (ADR-0032 Q32.d). "
        "The runner parses the last ```json fenced block to extract the verdict."
    )
    assert "```json" in prompt, (
        "role-architect prompt should include a literal ```json fence "
        "example so the model has a concrete template."
    )
    # Architect must teach all four verdict values.
    for value in ("amend", "supersede", "accept-as-is", "uncertain"):
        assert value in prompt, (
            f"role-architect prompt must reference verdict value {value!r}; "
            "the runner won't recognize verdicts the prompt doesn't teach."
        )


def test_role_architect_prompt_teaches_validator_tuning() -> None:
    """When the deadlock trigger is wf-validate.fail and verdict is
    accept-as-is, the architect must include a validator_tuning field in
    its JSON envelope. The prompt must teach the three action literals,
    the rule_slug field, and the proposed_patch shapes."""
    architect = next(r for r in _all_roles() if r["id"] == "role-architect")
    prompt = architect["system_prompt"]
    assert "validator_tuning" in prompt, (
        "role-architect prompt must teach the validator_tuning field "
        "for validate-fail accept-as-is verdicts"
    )
    for action in ("demote_severity", "narrow_applies_to", "refine_prompt"):
        assert action in prompt, (
            f"role-architect prompt must reference validator_tuning action "
            f"{action!r}; the disposition layer uses this literal to pick "
            "the rule-tuning path"
        )
    assert "rule_slug" in prompt, (
        "role-architect prompt must teach the rule_slug field so the "
        "disposition layer can look up which rule to tune"
    )


# Note: the prior contract tests asserted that every prompt mentioned
# the ``StepOutput`` envelope, listed decision values, and named
# ``task_directive`` for analyzer roles. Those tests encoded the
# pre-2026-05-12 belief that the worker parsed those structured fields
# from Claude's stdout — it doesn't (today the only parsed field is the
# VERDICT: marker for review-kind). The tests were locking in the lie.
# When structured step-output parsing lands (see ADR-0023 TBD or a
# sibling), the corresponding prompts + asserts come back together.


# ── ADR-0015 multi-step invariants ──────────────────────────────────────────


def _two_step_workflows() -> list[dict]:
    """Helper — every workflow with exactly two steps. Per ADR-0015's
    matrix this is ``wf-plan``, ``wf-feedback``, ``wf-ci-fix``,
    ``wf-conflict``; ADR-0034 adds ``wf-crystallize-learning``."""
    return [wf for wf in STARTERS if len(wf["steps"]) == 2]


def test_two_step_workflows_match_adr_0015_matrix() -> None:
    """Tripwire — the 2-step workflows are exactly those in ADR-0015's
    matrix plus ADR-0034's ``wf-crystallize-learning``. If this changes,
    the matrix moved and the test should update intentionally."""
    ids = {wf["id"] for wf in _two_step_workflows()}
    assert ids == {
        "wf-plan",
        "wf-feedback",
        "wf-ci-fix",
        "wf-conflict",
        "wf-crystallize-learning",
    }


def test_two_step_workflows_step_1_is_an_analyzer_class_role() -> None:
    """Per ADR-0015 — every 2-step workflow's step 1 is an analyzer-class
    role: a ``-analyzer`` suffix (feedback / ci / conflict),
    ``role-planner`` (wf-plan), or a ``-judge`` suffix (wf-crystallize-
    learning per ADR-0034). This is the analyzer-then-action shape's
    structural guarantee."""
    for wf in _two_step_workflows():
        step_1_role = wf["steps"][0]["role_id"]
        analyzer_class = (
            step_1_role.endswith("-analyzer")
            or step_1_role.endswith("-judge")
            or step_1_role == "role-planner"
        )
        assert analyzer_class, (
            f"workflow {wf['id']!r} step 1 role {step_1_role!r} is not "
            "analyzer-class (must end in '-analyzer' or '-judge', or be "
            "'role-planner')"
        )


def test_two_step_workflows_step_2_is_an_action_role() -> None:
    """Per ADR-0015 — every 2-step workflow's step 2 is one of the two
    action roles (``role-code-author`` for the resolution workflows;
    ``role-doc-author`` for wf-plan)."""
    for wf in _two_step_workflows():
        step_2_role = wf["steps"][1]["role_id"]
        assert step_2_role in _ACTION_ROLE_IDS, (
            f"workflow {wf['id']!r} step 2 role {step_2_role!r} is not "
            f"an action role (must be one of {sorted(_ACTION_ROLE_IDS)})"
        )


def test_role_code_author_is_the_shared_terminal() -> None:
    """Per ADR-0015 §"Role taxonomy" — ``role-code-author`` is referenced
    by exactly four workflows: wf-author (single-step), wf-feedback,
    wf-ci-fix, wf-conflict (each as the step-2 action). This is the
    bunkhouse-correcting reuse; if the count drifts, the consolidation
    has regressed."""
    workflows_using_code_author = {
        wf["id"]
        for wf in STARTERS
        for step in wf["steps"]
        if step["role_id"] == "role-code-author"
    }
    assert workflows_using_code_author == {
        "wf-author", "wf-feedback", "wf-ci-fix", "wf-conflict",
    }, (
        f"role-code-author should terminate exactly four workflows; "
        f"got {sorted(workflows_using_code_author)}"
    )


def test_every_role_is_referenced_by_at_least_one_workflow() -> None:
    """No orphan roles — every role defined in ``_ROLES`` must be used
    by at least one workflow's step. Catches dead-role drift if a future
    edit removes a workflow without also pruning its analyzer."""
    referenced_role_ids = {
        step["role_id"] for wf in STARTERS for step in wf["steps"]
    }
    defined_role_ids = {role["id"] for role in _all_roles()}
    orphans = defined_role_ids - referenced_role_ids
    assert not orphans, f"orphan roles (defined but unused): {sorted(orphans)}"


def test_single_step_workflows_match_adr_0015_and_0032_matrix() -> None:
    """Tripwire — the six single-step workflows are wf-author, wf-review,
    wf-validate, wf-doc-amend, wf-architecture-resolve, and
    wf-audit-rule-corpus. Per ADR-0015, ``wf-author`` deliberately stays
    single-step because its input is already structured. Per ADR-0032,
    ``wf-doc-amend`` and ``wf-architecture-resolve`` are single-step (verdict
    routing in disposition, not a second step)."""
    ids = {wf["id"] for wf in STARTERS if len(wf["steps"]) == 1}
    assert ids == {
        "wf-author", "wf-review", "wf-validate", "wf-doc-amend",
        "wf-architecture-resolve", "wf-audit-rule-corpus",
    }


# ── seed() behavior ──────────────────────────────────────────────────────────


class _StubApiClient:
    """In-memory stand-in for ``ApiClient._request``.

    Models the API's behavior closely enough for ``seed()`` to be tested
    without a live server:

      * ``POST /api/v1/roles`` and ``POST /api/v1/workflows`` 201 the first
        time a given slug is seen, 409 thereafter.
      * ``GET /api/v1/workflows/{id}`` returns ``latest_version`` (None
        until at least one version POSTs).
      * ``POST /api/v1/workflows/{id}/versions`` 201s every call and
        increments the recorded latest_version.
      * ``POST /api/v1/event-triggers`` 201 the first time a given
        ``(repo, event_type)`` pair is seen, 409 thereafter — mirrors
        the unique constraint at the schema level.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self._roles: set[str] = set()
        self._role_prompts: dict[str, str] = {}  # role_id → current system_prompt
        self._workflows: dict[str, dict] = {}  # id → {latest_version: int|None}
        self._triggers: set[tuple[str | None, str]] = set()  # (repo, event_type)

    def _request(self, method: str, path: str, **kwargs):
        from treadmill_cli.api_client import ApiError

        body = kwargs.get("json", {})
        self.calls.append((method, path, body))

        if method == "POST" and path == "/api/v1/roles":
            role_id = body.get("id")
            if role_id in self._roles:
                raise ApiError(409, f"role {role_id!r} exists")
            self._roles.add(role_id)
            self._role_prompts[role_id] = body.get("system_prompt", "")
            return {"id": role_id}

        if method == "PATCH" and path.startswith("/api/v1/roles/"):
            role_id = path.rsplit("/", 1)[-1]
            if role_id not in self._roles:
                raise ApiError(404, f"role {role_id!r} not found")
            self._role_prompts[role_id] = body.get("system_prompt", "")
            return {"role": {"id": role_id}, "version": 2}

        if method == "POST" and path == "/api/v1/workflows":
            wf_id = body.get("id")
            if wf_id in self._workflows:
                raise ApiError(409, f"workflow {wf_id!r} exists")
            self._workflows[wf_id] = {"latest_version": None}
            return {"id": wf_id, "latest_version": None}

        if method == "GET" and path.startswith("/api/v1/workflows/"):
            wf_id = path.rsplit("/", 1)[-1]
            data = self._workflows.get(wf_id)
            if data is None:
                raise ApiError(404, f"{wf_id!r} not found")
            return {"id": wf_id, **data}

        if (
            method == "POST"
            and path.startswith("/api/v1/workflows/")
            and path.endswith("/versions")
        ):
            wf_id = path.split("/")[-2]
            data = self._workflows.setdefault(wf_id, {"latest_version": None})
            next_v = (data["latest_version"] or 0) + 1
            data["latest_version"] = next_v
            return {"id": "v", "version": next_v}

        if method == "POST" and path == "/api/v1/event-triggers":
            key = (body.get("repo"), body.get("event_type"))
            if key in self._triggers:
                raise ApiError(
                    409, f"trigger for {key!r} exists",
                )
            self._triggers.add(key)
            return {"id": "t", **body}

        raise AssertionError(f"unexpected request: {method} {path}")


def test_seed_posts_every_role_and_workflow_on_clean_install() -> None:
    """First-time seed POSTs every role, workflow, and v1 version."""
    from treadmill_api.starters import seed

    client = _StubApiClient()
    result = seed(client)

    # Every workflow was freshly created.
    assert result.fresh_workflows == len(STARTERS)
    # No prompts reset on a clean install — there were no existing
    # role rows to PATCH against.
    assert result.role_prompts_reset == []

    # Roles POSTed first — exactly one per declared role (the de-dup
    # collapses the four references to ``role-code-author`` into one
    # POST).
    role_posts = [
        c for c in client.calls
        if c[0] == "POST" and c[1] == "/api/v1/roles"
    ]
    assert len(role_posts) == len(_all_roles())

    # Each workflow POSTed once.
    wf_posts = [
        c for c in client.calls
        if c[0] == "POST" and c[1] == "/api/v1/workflows"
    ]
    assert len(wf_posts) == len(STARTERS)

    # Each workflow got exactly one v1 version POSTed.
    version_posts = [
        c for c in client.calls
        if c[0] == "POST" and c[1].endswith("/versions")
    ]
    assert len(version_posts) == len(STARTERS)


def test_seed_is_idempotent_on_re_run() -> None:
    """A re-run against an already-seeded install: returns ``0`` newly-
    created workflows, does NOT create new versions (versions auto-
    increment, so a non-checking re-run would inflate the count)."""
    from treadmill_api.starters import seed

    client = _StubApiClient()
    seed(client)  # first run primes everything
    pre_second_run_versions = {
        wf_id: data["latest_version"]
        for wf_id, data in client._workflows.items()
    }

    result = seed(client)

    # No workflows created on the second run.
    assert result.fresh_workflows == 0
    # Default (reset_prompts_from_code=False) leaves prompts alone.
    assert result.role_prompts_reset == []
    # No new versions either — the GET-before-POST guard kicked in.
    for wf in STARTERS:
        assert client._workflows[wf["id"]]["latest_version"] == pre_second_run_versions[wf["id"]]


def test_seed_creates_v1_when_workflow_exists_without_versions() -> None:
    """Edge case: a half-seeded install (workflow row present, no version
    yet) gets its v1 created on a re-run, but the workflow is NOT counted
    as freshly created."""
    from treadmill_api.starters import seed

    client = _StubApiClient()
    # Pre-seed just one workflow row, no version.
    client._workflows["wf-author"] = {"latest_version": None}

    result = seed(client)
    # wf-author was already there → not counted as fresh; the other six are.
    assert result.fresh_workflows == len(STARTERS) - 1
    # And the half-seeded one received its v1.
    assert client._workflows["wf-author"]["latest_version"] == 1


def test_seed_default_does_not_overwrite_existing_role_prompts() -> None:
    """ADR-0028 default behavior: a re-run against an already-seeded
    install with ``reset_prompts_from_code=False`` (the default) does
    NOT PATCH any role prompts back — operator edits via 'treadmill
    role update' are preserved."""
    from treadmill_api.starters import seed

    client = _StubApiClient()
    seed(client)  # first run primes everything
    # Operator edits a role's prompt via PATCH (simulated by direct
    # state mutation; real-world this happens via 'treadmill role
    # update'). The default re-run should leave this untouched.
    client._role_prompts["role-code-author"] = "OPERATOR EDIT"

    result = seed(client)

    assert result.role_prompts_reset == []
    # The operator edit is preserved.
    assert client._role_prompts["role-code-author"] == "OPERATOR EDIT"
    # No PATCH was issued by the default seed.
    patches = [c for c in client.calls if c[0] == "PATCH"]
    assert patches == []


def test_seed_reset_prompts_from_code_patches_existing_roles() -> None:
    """ADR-0028 recovery path: ``reset_prompts_from_code=True``
    overwrites every existing role's system_prompt with the code-side
    definition. Returns the list of role ids that were reset."""
    from treadmill_api.starters import _all_roles, seed

    client = _StubApiClient()
    seed(client)  # prime everything
    # Operator drift on one role.
    client._role_prompts["role-code-author"] = "DRIFTED EDIT"

    result = seed(client, reset_prompts_from_code=True)

    # Every role's POST returned 409, so every role got PATCHed.
    expected_role_ids = {r["id"] for r in _all_roles()}
    assert set(result.role_prompts_reset) == expected_role_ids
    # The drifted edit was overwritten back to the code-side definition.
    code_prompts = {r["id"]: r["system_prompt"] for r in _all_roles()}
    assert (
        client._role_prompts["role-code-author"]
        == code_prompts["role-code-author"]
    )


def test_seed_posts_role_code_author_only_once() -> None:
    """Per ADR-0015 — ``role-code-author`` is referenced by four
    workflows; ``_all_roles()`` de-duplicates so ``seed()`` POSTs it
    exactly once. Verifies the dedup is honest at the network layer."""
    from treadmill_api.starters import seed

    client = _StubApiClient()
    seed(client)
    code_author_posts = [
        c for c in client.calls
        if c[0] == "POST"
        and c[1] == "/api/v1/roles"
        and c[2].get("id") == "role-code-author"
    ]
    assert len(code_author_posts) == 1


def test_seed_posts_default_event_triggers() -> None:
    """Per Week-3 plan §C.2, ``seed()`` ensures the five default
    catch-all ``event_triggers`` rows exist (in addition to alembic
    migration 0007 which does the same on schema upgrade). Idempotent:
    a re-run produces zero extra POSTs against the trigger endpoint."""
    from treadmill_api.starters import _DEFAULT_EVENT_TRIGGERS, seed

    client = _StubApiClient()
    seed(client)
    trigger_posts = [
        c for c in client.calls
        if c[0] == "POST" and c[1] == "/api/v1/event-triggers"
    ]
    # One POST per default trigger.
    assert len(trigger_posts) == len(_DEFAULT_EVENT_TRIGGERS)
    # Each maps to one of the expected (event_type, workflow_id) pairs.
    posted_pairs = {(c[2]["event_type"], c[2]["workflow_id"]) for c in trigger_posts}
    assert posted_pairs == set(_DEFAULT_EVENT_TRIGGERS)

    # Re-run: existing triggers 409 silently; no fresh POSTs land in
    # the recorded state (the 409 still counts as a call, so we check
    # the underlying set rather than the call log).
    pre_second_run = set(client._triggers)
    seed(client)
    assert client._triggers == pre_second_run


# ── ADR-0022 workflow-shape validator ────────────────────────────────────────


def test_validate_workflow_shapes_accepts_canonical_starters() -> None:
    """The seven canonical starter workflows must pass the static
    shape check. If this test fails the validator is too strict — the
    starters define the v0 reference shape per ADR-0015 + ADR-0022."""
    # Should return without raising. ``_validate_workflow_shapes`` is
    # called from ``seed()`` at the top so a passing call means the
    # canonical mix is acceptable.
    _validate_workflow_shapes()


def test_validate_workflow_shapes_rejects_review_first_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workflow whose first step is a review-kind role (other than
    wf-review itself) must be rejected — there's no PR yet for the
    reviewer to look at."""
    from treadmill_api import starters

    bad = [
        {
            "id": "wf-author",  # not wf-review, so review-first is invalid
            "description": "bad shape",
            "roles": [{"id": "role-reviewer", "output_kind": OutputKind.REVIEW}],
            "steps": [{"name": "review", "role_id": "role-reviewer"}],
        },
    ]
    monkeypatch.setattr(starters, "STARTERS", bad)
    with pytest.raises(WorkflowShapeError, match="review-kind"):
        _validate_workflow_shapes()


def test_validate_workflow_shapes_rejects_plan_doc_outside_wf_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plan_doc-kind step in a workflow other than wf-plan is a
    constraint violation — the docs/plans/ confinement is wf-plan-
    specific."""
    from treadmill_api import starters

    bad = [
        {
            "id": "wf-author",
            "description": "bad shape",
            "roles": [{"id": "role-doc-author", "output_kind": OutputKind.PLAN_DOC}],
            "steps": [{"name": "author", "role_id": "role-doc-author"}],
        },
    ]
    monkeypatch.setattr(starters, "STARTERS", bad)
    with pytest.raises(WorkflowShapeError, match="plan_doc"):
        _validate_workflow_shapes()


def test_validate_workflow_shapes_rejects_undefined_role_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A step that references a role not in ``_ROLES`` is a wiring bug."""
    from treadmill_api import starters

    bad = [
        {
            "id": "wf-author",
            "description": "bad shape",
            "roles": [],
            "steps": [{"name": "step", "role_id": "role-ghost"}],
        },
    ]
    monkeypatch.setattr(starters, "STARTERS", bad)
    with pytest.raises(WorkflowShapeError, match="undefined role"):
        _validate_workflow_shapes()


def test_seed_default_event_triggers_match_week3_plan() -> None:
    """The exact (event_type → workflow_id) mappings per Week-3 plan
    §C.2. Tripwire — keeps the seed in sync with the table in the plan
    and with ``coordination/triggers.py`` cap policies."""
    from treadmill_api.starters import _DEFAULT_EVENT_TRIGGERS

    expected = [
        ("pr_opened", "wf-review"),
        ("pr_synchronize", "wf-review"),
        ("pr_review_submitted", "wf-feedback"),
        ("check_run_completed", "wf-ci-fix"),
        ("pr_conflict", "wf-conflict"),
    ]
    assert _DEFAULT_EVENT_TRIGGERS == expected


# ── role-rule-corpus-auditor + RuleCorpusAudit ───────────────────────────────


def test_role_rule_corpus_auditor_prompt_teaches_json_envelope() -> None:
    """The rule-corpus-auditor role must teach the JSON envelope with the
    ``entries`` list and the three status literals (keep / deprecate / update).
    The disposition layer parses the fenced JSON block to extract the audit
    results; without envelope teaching the model produces prose and the
    parse fails."""
    auditor = next(r for r in _all_roles() if r["id"] == "role-rule-corpus-auditor")
    prompt = auditor["system_prompt"]
    assert "json" in prompt.lower(), (
        "role-rule-corpus-auditor prompt must teach the JSON envelope"
    )
    assert "```json" in prompt, (
        "role-rule-corpus-auditor prompt must include a literal ```json fence "
        "example so the model has a concrete template"
    )
    assert "entries" in prompt, (
        "role-rule-corpus-auditor prompt must reference the 'entries' field "
        "— the envelope is a list of per-rule results"
    )
    for status in ("keep", "deprecate", "update"):
        assert status in prompt, (
            f"role-rule-corpus-auditor prompt must reference status value {status!r}; "
            "the disposition layer uses these literals to route follow-up actions"
        )


def test_rule_corpus_audit_envelope_importable_from_events() -> None:
    """``RuleCorpusAudit`` and ``RuleCorpusAuditEntry`` must be importable
    from ``treadmill_api.events`` so the disposition layer can parse the
    auditor's JSON output into a typed envelope without importing from the
    internal sub-module directly."""
    from treadmill_api.events import RuleCorpusAudit, RuleCorpusAuditEntry

    # Smoke-test that the model accepts valid input.
    entry = RuleCorpusAuditEntry(
        rule_slug="adr-and-plan-has-diagram",
        status="keep",
        rationale="Rule is referenced and check.sh exists.",
        proposed_action="no action",
    )
    audit = RuleCorpusAudit(entries=[entry])
    assert audit.entries[0].status == "keep"
    assert audit.entries[0].rule_slug == "adr-and-plan-has-diagram"
