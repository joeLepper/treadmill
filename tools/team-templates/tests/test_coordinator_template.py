"""Structural tests for the ADR-0087 coordinator CLAUDE.md template.

These tests pin the SHAPE of the coordinator prompt — the presence of
load-bearing sections and the specific magic numbers / vocabulary the
rest of the system depends on. They do NOT judge prose quality (the
operator owns that); they fail loudly if a future edit accidentally
drops a section that another part of the system contracts on.

Coverage axes:
  - Placeholder substitution: ``{{REPO_SLUG}}`` appears in the template
    and resolves through ``_render``.
  - Required handler sections: each of the lifecycle handlers named in
    ADR-0087 §Task execution flow is present.
  - Single-writer invariant + trust-boundary language (ADR-0087
    §Decision + §Security considerations).
  - The four-value trigger taxonomy is named exactly.
  - The evaluator-timeout magic numbers (30 / 60 minutes) match the
    ADR.
  - The max-cycles cap (≥3 evaluator-rework) matches the ADR.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


# Make the install module importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
TEMPLATES_DIR = Path(__file__).resolve().parent.parent

from install import (  # noqa: E402
    make_team_spec,
    install_team,
)


_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "coordinator" / "CLAUDE.md.tmpl"
)


# ── Template-source assertions (no install needed) ──────────────────


def test_template_file_exists() -> None:
    assert _TEMPLATE_PATH.is_file(), (
        "expected tools/team-templates/coordinator/CLAUDE.md.tmpl to exist"
    )


def test_template_carries_repo_slug_placeholder() -> None:
    body = _TEMPLATE_PATH.read_text()
    assert "{{REPO_SLUG}}" in body


def test_template_does_not_carry_worker_label_placeholder() -> None:
    """``{{WORKER_LABEL}}`` is the worker-template placeholder; the
    coordinator template has exactly one coordinator per team and
    must not reference it."""
    body = _TEMPLATE_PATH.read_text()
    assert "{{WORKER_LABEL}}" not in body


# ── Required handler sections ───────────────────────────────────────


_REQUIRED_HANDLERS = (
    "plan.submitted",
    "task.registered",
    "github.pr_merged",
    "task.ci_result",
    "github.pull_request.synchronize",
)


@pytest.mark.parametrize("handler", _REQUIRED_HANDLERS)
def test_template_names_required_handler(handler: str) -> None:
    """ADR-0087 §Task execution flow + §Coordinator failure recovery
    name these handlers as load-bearing. The template must reference
    each so a downstream contributor doesn't accidentally drop one."""
    body = _TEMPLATE_PATH.read_text()
    assert handler in body, (
        f"handler {handler!r} missing from coordinator template — "
        "ADR-0087 lifecycle contracts on it being present"
    )


# ── Architectural invariants ────────────────────────────────────────


def test_template_declares_single_writer_invariant() -> None:
    """The coordinator is the sole writer of ``task_executions``
    rows (ADR-0087 §Decision). The template must carry this
    declaration so a future edit doesn't accidentally invite
    multi-writer behaviour."""
    body = _TEMPLATE_PATH.read_text().lower()
    assert "single writer" in body


def test_template_declares_relay_trust_boundary() -> None:
    """Per ADR-0087 §Security considerations, only relay messages
    with ``[from: coordinator-<slug>]`` headers are trusted as
    instructions. The template must reinforce this when composing
    briefs to workers."""
    body = _TEMPLATE_PATH.read_text()
    assert "[from: coordinator-" in body
    assert "trusted" in body.lower() or "trust" in body.lower()


# ── Trigger taxonomy ────────────────────────────────────────────────


_VALID_TRIGGERS = (
    "initial",
    "coordinator-rework",
    "evaluator-rework",
    "peer-review",
)


@pytest.mark.parametrize("trigger", _VALID_TRIGGERS)
def test_template_names_valid_trigger(trigger: str) -> None:
    """ADR-0087 §Rework tracking pins the four-value trigger taxonomy
    + the SQL queries depend on the names. The template must use the
    exact strings so an in-template dispatch description doesn't
    drift from the schema."""
    body = _TEMPLATE_PATH.read_text()
    assert trigger in body, f"trigger {trigger!r} missing from template"


# ── Magic numbers ───────────────────────────────────────────────────


def test_template_carries_evaluator_timeout_numbers() -> None:
    """30 / 60 minutes per ADR-0087 §Task execution flow §6. The
    template must name them so the runtime behaviour matches the
    spec."""
    body = _TEMPLATE_PATH.read_text()
    assert "30 minutes" in body
    assert "60 minutes" in body


def test_template_carries_max_evaluator_rework_cap() -> None:
    """≥3 evaluator-rework rows triggers orchestrator escalation per
    ADR-0087 §Task execution flow §6 (Max-cycles cap)."""
    body = _TEMPLATE_PATH.read_text()
    assert "≥3" in body or ">= 3" in body or "≥ 3" in body


def test_template_carries_mergeability_polling_bound() -> None:
    """ADR-0087 §CI and conflict signals pins the mergeability poll
    at 10s intervals, max 30 attempts (5 min). The template must
    reflect this so the coordinator's runtime behaviour matches."""
    body = _TEMPLATE_PATH.read_text()
    assert "10 seconds" in body or "10s" in body
    assert "30 attempts" in body
    assert "mergeability_undetermined" in body


# ── Startup recovery ────────────────────────────────────────────────


def test_template_carries_startup_recovery_section() -> None:
    """ADR-0087 §Coordinator failure recovery + Donna's review fold
    pin the four startup-recovery steps. The template must enumerate
    them so a restarted coordinator does not skip safety sweeps."""
    body = _TEMPLATE_PATH.read_text().lower()
    assert "startup" in body
    # Each of the four recovery steps must be named.
    assert "stale" in body  # stale running task_executions sweep
    assert "drain" in body  # relay inbox drain
    assert "re-poll" in body or "repoll" in body  # mergeability re-poll
    assert "replay" in body  # events-table plan.submitted replay


# ── End-to-end render check ─────────────────────────────────────────


def test_install_resolves_repo_slug_in_rendered_template(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """install_team renders the coordinator template with
    ``{{REPO_SLUG}}`` substituted. The placeholder must NOT appear in
    the on-disk file or the live coordinator session reads a literal
    `{{REPO_SLUG}}` string and routes wrong."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # _TEAMS_ROOT + _SHARED_TEMPLATES_DIR are captured at import time;
    # patch on the module the same way Carla's synthetic_home fixture
    # does in test_install.py.
    import install as _install

    monkeypatch.setattr(
        _install, "_TEAMS_ROOT", tmp_path / ".treadmill" / "teams"
    )
    monkeypatch.setattr(
        _install,
        "_SHARED_TEMPLATES_DIR",
        tmp_path / ".treadmill" / "teams" / "__templates__",
    )

    spec = make_team_spec("medicoder", worker_count=2)
    install_team(spec)
    body = (
        tmp_path / ".treadmill" / "teams" / "medicoder"
        / "coordinator-medicoder" / "CLAUDE.md"
    ).read_text()
    assert "{{REPO_SLUG}}" not in body
    assert "coordinator-medicoder" in body
    # Sanity: the substituted slug appears in the per-team header / labels.
    assert "medicoder" in body


def test_template_pins_deploy_observe_section() -> None:
    """§3.7 deploy/smoke observe-and-escalate survives the 2026-06-11
    ADR-0088 unwind (team telemetry, not deploy control). The promotion
    §3.8 must NOT exist — deploy approval is GitHub environment
    protection, the repo's own CI concern (operator directive)."""
    body = (TEMPLATES_DIR / "coordinator" / "CLAUDE.md.tmpl").read_text()
    assert "deploy-gating and merge-gating stay decoupled" in body
    assert "### 3.7" in body
    assert "### 3.8" not in body
    assert "prod_promotion" not in body
    assert "X-Operator-Key" not in body


def test_worker_template_pins_brief_contract_and_pr_number_convention() -> None:
    """The brief is the contract (API reads never load-bearing) + the
    PR-number-via-separate-command convention (2026-06-10 incidents)."""
    body = (TEMPLATES_DIR / "worker" / "CLAUDE.md.tmpl").read_text()
    assert "your BRIEF is the contract" in body
    assert "gh pr view --json number" in body


# ── ADR-0089 §3: cache-aware cadence convention (task 4fce76d5) ──────


@pytest.mark.parametrize("role", ["coordinator", "worker", "evaluator"])
def test_templates_pin_cadence_convention(role: str) -> None:
    """Every session template carries the ADR-0089 §3 cadence rule with
    its three load-bearing numbers — poll inside the ~5-min prompt-cache
    window when actively watching, long intervals otherwise, never the
    worst-of-both middle — and cites the live wake-filter mechanism
    (#310) it layers under."""
    # Whitespace-normalized so a phrase spanning a hard line wrap in
    # the template prose still pins.
    body = " ".join(
        (TEMPLATES_DIR / role / "CLAUDE.md.tmpl").read_text().split()
    )
    assert "ADR-0089" in body
    assert "poll inside the cache window" in body
    assert "≤270s" in body
    assert "long intervals (≥20 min)" in body
    assert "~5-minute middle" in body
    # The convention text cites the live ADR-0089 wake-filter mechanism.
    assert "TREADMILL_WAKE_ACTIONS" in body
    assert "suppression digest" in body


def test_evaluator_template_pins_batch_per_wake() -> None:
    """The bursty-but-rare role's half of the convention: batch the
    queue per wake instead of waking per PR (ADR-0089 §3)."""
    body = " ".join(
        (TEMPLATES_DIR / "evaluator" / "CLAUDE.md.tmpl").read_text().split()
    )
    assert "batch work when woken" in body
    assert "every queued PR in one wake" in body


# ── Workspace isolation (task 801256e3) ──────────────────────────────


def test_worker_template_pins_workspace_isolation() -> None:
    """The 2026-06-11 collision rule (worker files swept into an
    orchestrator revert commit): workers implement in their OWN clone
    under their team dir and never write to the orchestrators' shared
    host worktrees. Whitespace-normalized so the hard-wrapped prose
    still pins."""
    body = " ".join(
        (TEMPLATES_DIR / "worker" / "CLAUDE.md.tmpl").read_text().split()
    )
    assert "## Workspace isolation" in body
    assert "your own clone" in body
    # The verified clone-path convention (template form).
    assert "~/.treadmill/teams/{{REPO_SLUG}}/{{WORKER_LABEL}}/<repo>" in body
    assert "NEVER write to `/home/joe/<repo>`" in body
    # The one-line WHY the rule exists.
    assert "four orchestrator sessions run branch operations there daily" in body
    # The workdir trap: a pointer FILE owned by the launcher, not a dir.
    assert "~/.cc-channels/{{WORKER_LABEL}}/workdir" in body
    assert "pointer FILE" in body


def test_install_renders_isolation_clone_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VERIFY clause of task 801256e3: the pinned isolation lines are
    present in a rendered install, with the clone-path convention
    resolved to the worker's real team dir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    import install as _install

    monkeypatch.setattr(
        _install, "_TEAMS_ROOT", tmp_path / ".treadmill" / "teams"
    )
    monkeypatch.setattr(
        _install,
        "_SHARED_TEMPLATES_DIR",
        tmp_path / ".treadmill" / "teams" / "__templates__",
    )

    spec = make_team_spec("medicoder", worker_count=1)
    install_team(spec)
    body = " ".join(
        (
            tmp_path / ".treadmill" / "teams" / "medicoder"
            / "worker-medicoder-1" / "CLAUDE.md"
        ).read_text().split()
    )
    assert "~/.treadmill/teams/medicoder/worker-medicoder-1/<repo>" in body
    assert "NEVER write to `/home/joe/<repo>`" in body
    assert "{{WORKER_LABEL}}" not in body


# ── ADR-0090: §3.5 ci_result rollup handler (task 257b19a2) ──────────


def _coordinator_plain() -> str:
    return " ".join(
        (TEMPLATES_DIR / "coordinator" / "CLAUDE.md.tmpl").read_text().split()
    )


def test_per_check_advance_rework_block_is_gone() -> None:
    """The DELETION is a success criterion (plan cb3d0c29): the old
    §3.5 per-check handler — advance on the last required check,
    rework per check, coordinator hand-writing task.ci_result — must
    not survive in any form."""
    body = _coordinator_plain()
    assert "### 3.5 `github.check_run.completed`" not in body
    assert "last required check" not in body
    assert "Write a `task.ci_result` event via `POST /api/v1/events`" not in body
    # check_run survives ONLY as the never-re-derive rule + the
    # transition note (no handler steps attached).
    assert "They require NO action" in body


def test_ci_result_handler_pins_payload_and_decisions() -> None:
    body = _coordinator_plain()
    assert "### 3.5 `task.ci_result`" in body
    # The #336 payload contract, verbatim fields.
    assert (
        "`{repo, pr_number, head_sha, check_suite_id, conclusion, app_slug}`"
        in body
    )
    assert "ONE decision per rollup" in body
    assert "trigger peer review per §8" in body
    assert "open coordinator-rework per §7.1" in body


def test_ci_result_handler_pins_the_four_contractual_carry_forwards() -> None:
    """#336 review+evaluation carry-forwards, contractual in the
    handler text (task 257b19a2)."""
    body = _coordinator_plain()
    # (1) terminal-task tolerance, written down — not folklore.
    assert "closed-PR heads DO emit" in body
    assert "This tolerance is CONTRACTUAL, not folklore" in body
    # (2) per-suite cardinality + the app filter as consumer policy.
    assert "two suites at one head = two ci_result events" in body
    assert "app_slug == 'github-actions'" in body
    # (3) the serialized-ingest dedup caveat.
    assert "holds only while API ingest stays serialized" in body
    assert "keep this handler idempotent" in body
    # (4) repo case-matching intent.
    assert "canonical GitHub `owner/name` casing" in body


def test_coordinator_does_not_write_ci_result() -> None:
    """The observer owns the event; a coordinator write would collide
    with its idempotency key."""
    body = _coordinator_plain()
    assert "You do NOT write this event" in body
