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
    "github.check_run.completed",
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
