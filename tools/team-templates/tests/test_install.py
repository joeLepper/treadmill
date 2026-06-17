"""Tests for the team-template installer (ADR-0087 PR-E)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


# Make the install module importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from install import (  # noqa: E402
    TeamSpec,
    install_shared_templates,
    install_team,
    make_team_spec,
)


# ── TeamSpec derivation ──────────────────────────────────────────────


def test_make_team_spec_default_worker_count() -> None:
    spec = make_team_spec("medicoder")
    assert spec.repo_slug == "medicoder"
    assert spec.coordinator_label == "coordinator-medicoder"
    assert spec.evaluator_label == "evaluator-medicoder"
    assert spec.worker_count == 3
    assert spec.worker_labels == (
        "worker-medicoder-1",
        "worker-medicoder-2",
        "worker-medicoder-3",
    )


def test_make_team_spec_custom_worker_count() -> None:
    spec = make_team_spec("scraper-v2", worker_count=5)
    assert spec.worker_count == 5
    assert len(spec.worker_labels) == 5
    assert spec.worker_labels[-1] == "worker-scraper-v2-5"


def test_make_team_spec_is_deterministic() -> None:
    """Same inputs → same spec. Idempotent ``team up`` depends on this."""
    spec_a = make_team_spec("medicoder", worker_count=3)
    spec_b = make_team_spec("medicoder", worker_count=3)
    assert spec_a == spec_b


def test_make_team_spec_pr_base_defaults_to_main() -> None:
    """Existing teams (medicoder/treadmill) keep main — no behavior change."""
    assert make_team_spec("medicoder").pr_base == "main"


def test_make_team_spec_custom_pr_base() -> None:
    """A team can target a non-default trunk (osmo → forecast/stage-a)."""
    assert make_team_spec("osmo", pr_base="forecast/stage-a").pr_base == "forecast/stage-a"


def test_render_substitutes_pr_base(tmp_path: Path) -> None:
    """``{{PR_BASE}}`` is replaced with the team's base — and nothing is
    left un-substituted (a leftover placeholder would have the worker
    branch from a literal '{{PR_BASE}}')."""
    from install import _render

    src = tmp_path / "t.tmpl"
    src.write_text("branch from origin/{{PR_BASE}}; gh pr create --base {{PR_BASE}}")
    dst = tmp_path / "out.md"
    _render(src, dst, make_team_spec("osmo", pr_base="forecast/stage-a"))
    body = dst.read_text()
    assert "origin/forecast/stage-a" in body
    assert "--base forecast/stage-a" in body
    assert "{{PR_BASE}}" not in body


def test_render_pr_base_defaults_to_main(tmp_path: Path) -> None:
    from install import _render

    src = tmp_path / "t.tmpl"
    src.write_text("origin/{{PR_BASE}}")
    dst = tmp_path / "out.md"
    _render(src, dst, make_team_spec("medicoder"))
    assert dst.read_text() == "origin/main"


# ── install_shared_templates ─────────────────────────────────────────


def test_install_shared_templates_copies_hook_script(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    install_shared_templates(dest_root=tmp_path / "templates")
    hook = tmp_path / "templates" / "worker" / "relay_inject_hook.py"
    assert hook.is_file()
    # Sanity: it's a Python file with the expected entry point.
    body = hook.read_text()
    assert "def main()" in body
    assert "decision" in body


def test_install_shared_templates_copies_template_sources(
    tmp_path: Path,
) -> None:
    install_shared_templates(dest_root=tmp_path / "templates")
    assert (
        tmp_path / "templates" / "worker" / "settings.json.tmpl"
    ).is_file()
    assert (
        tmp_path / "templates" / "worker" / "CLAUDE.md.tmpl"
    ).is_file()
    assert (
        tmp_path / "templates" / "evaluator" / "CLAUDE.md.tmpl"
    ).is_file()


# ── install_team renders per-session config ──────────────────────────


@pytest.fixture
def synthetic_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Redirect $HOME so install_team writes into tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # The install module captures _TEAMS_ROOT at import time from
    # Path.home() — patch the module-level constant for this test.
    import install
    monkeypatch.setattr(
        install, "_TEAMS_ROOT", tmp_path / ".treadmill" / "teams"
    )
    monkeypatch.setattr(
        install,
        "_SHARED_TEMPLATES_DIR",
        tmp_path / ".treadmill" / "teams" / "__templates__",
    )
    return tmp_path


def test_install_team_writes_evaluator_claude_md(
    synthetic_home: Path,
) -> None:
    spec = make_team_spec("medicoder", worker_count=2)
    install_team(spec)

    claude = (
        synthetic_home
        / ".treadmill"
        / "teams"
        / "medicoder"
        / "evaluator-medicoder"
        / "CLAUDE.md"
    )
    assert claude.is_file()
    body = claude.read_text()
    # Placeholder substituted.
    assert "evaluator-medicoder" in body
    assert "{{REPO_SLUG}}" not in body
    # Fixed verdict format preserved.
    assert "[verdict: approve | rework]" in body


def test_install_team_writes_one_worker_config_per_worker(
    synthetic_home: Path,
) -> None:
    spec = make_team_spec("medicoder", worker_count=3)
    install_team(spec)

    team_dir = (
        synthetic_home / ".treadmill" / "teams" / "medicoder"
    )
    for n in (1, 2, 3):
        worker_dir = team_dir / f"worker-medicoder-{n}"
        assert worker_dir.is_dir()
        claude = worker_dir / "CLAUDE.md"
        # settings.json now lives under <label>/.claude/ so Claude
        # Code's project-settings discovery (cwd/.claude/settings.json)
        # actually picks it up. Pre-ADR-0087-PR-H, it landed bare at
        # <label>/settings.json — read by nothing.
        settings = worker_dir / ".claude" / "settings.json"
        assert claude.is_file()
        assert settings.is_file()

        # Placeholders substituted per worker.
        claude_body = claude.read_text()
        assert f"worker-medicoder-{n}" in claude_body
        assert "{{WORKER_LABEL}}" not in claude_body
        assert "{{REPO_SLUG}}" not in claude_body

        # settings.json is valid JSON (despite the {{ ... }} placeholders
        # in the .tmpl — the worker template doesn't actually use them
        # today; render is a no-op, but verify parse).
        settings_body = settings.read_text()
        # Strip the JSON $comment keys our shape uses (they're valid
        # JSON keys, not actual comments — just convention).
        parsed = json.loads(settings_body)
        assert "permissions" in parsed
        assert "hooks" in parsed
        assert "PostToolUse" in parsed["hooks"]


def test_install_team_is_idempotent(synthetic_home: Path) -> None:
    """Re-installing the same spec overwrites cleanly without raising."""
    spec = make_team_spec("medicoder", worker_count=2)
    install_team(spec)
    install_team(spec)  # second invocation must not raise

    claude = (
        synthetic_home
        / ".treadmill"
        / "teams"
        / "medicoder"
        / "worker-medicoder-1"
        / "CLAUDE.md"
    )
    assert claude.is_file()


def test_install_team_no_cross_team_overwrite(
    synthetic_home: Path,
) -> None:
    """Installing two teams creates two independent directory trees."""
    install_team(make_team_spec("medicoder", worker_count=1))
    install_team(make_team_spec("scraper-v2", worker_count=1))

    medicoder_dir = (
        synthetic_home / ".treadmill" / "teams" / "medicoder"
    )
    scraper_dir = (
        synthetic_home / ".treadmill" / "teams" / "scraper-v2"
    )
    assert (medicoder_dir / "worker-medicoder-1" / "CLAUDE.md").is_file()
    assert (scraper_dir / "worker-scraper-v2-1" / "CLAUDE.md").is_file()

    # Cross-pollination check: medicoder worker's CLAUDE.md doesn't
    # accidentally reference scraper-v2.
    medi_body = (
        medicoder_dir / "worker-medicoder-1" / "CLAUDE.md"
    ).read_text()
    assert "scraper-v2" not in medi_body


def test_install_team_renders_coordinator_claude_md(
    synthetic_home: Path,
) -> None:
    """ADR-0087 PR-D — install_team now writes the coordinator's
    rendered CLAUDE.md alongside the worker + evaluator ones. The
    file lands at
    ``~/.treadmill/teams/<slug>/coordinator-<slug>/CLAUDE.md`` with
    ``{{REPO_SLUG}}`` resolved to the team's slug. No settings.json
    is written (the coordinator does not run the worker's PostToolUse
    hook substrate — it IS the live process routing signals)."""
    spec = make_team_spec("medicoder", worker_count=2)
    install_team(spec)

    coord_dir = (
        synthetic_home
        / ".treadmill"
        / "teams"
        / "medicoder"
        / "coordinator-medicoder"
    )
    assert coord_dir.is_dir()
    coord_claude = coord_dir / "CLAUDE.md"
    assert coord_claude.is_file()
    body = coord_claude.read_text()
    assert "coordinator-medicoder" in body  # substituted REPO_SLUG appears in the label form
    assert "medicoder" in body
    assert "{{REPO_SLUG}}" not in body  # placeholder fully resolved
    # Coordinator-specific: no settings.json next to it (workers only).
    assert not (coord_dir / "settings.json").exists()


def test_install_team_coordinator_does_not_carry_worker_label_placeholder(
    synthetic_home: Path,
) -> None:
    """Coordinator template references ``{{REPO_SLUG}}`` only — the
    ``{{WORKER_LABEL}}`` placeholder used in worker templates must not
    leak into the coordinator's rendered output."""
    spec = make_team_spec("scraper-v2", worker_count=3)
    install_team(spec)

    body = (
        synthetic_home
        / ".treadmill"
        / "teams"
        / "scraper-v2"
        / "coordinator-scraper-v2"
        / "CLAUDE.md"
    ).read_text()
    assert "{{WORKER_LABEL}}" not in body


def test_install_team_coordinator_is_idempotent(synthetic_home: Path) -> None:
    """Re-running ``install_team`` overwrites the coordinator's
    rendered CLAUDE.md cleanly (same shape ``test_install_team_is_
    idempotent`` pins for worker + evaluator)."""
    spec = make_team_spec("medicoder", worker_count=2)
    install_team(spec)
    first = (
        synthetic_home
        / ".treadmill"
        / "teams"
        / "medicoder"
        / "coordinator-medicoder"
        / "CLAUDE.md"
    ).read_text()
    install_team(spec)
    second = (
        synthetic_home
        / ".treadmill"
        / "teams"
        / "medicoder"
        / "coordinator-medicoder"
        / "CLAUDE.md"
    ).read_text()
    assert first == second


def test_install_shared_templates_includes_coordinator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared template dir picks up coordinator/ alongside
    worker/ + evaluator/. AGENT.md (editor-facing) is skipped on the
    copy so the deployed __templates__ tree stays minimal."""
    monkeypatch.setenv("HOME", str(tmp_path))
    install_shared_templates(
        dest_root=tmp_path / ".treadmill" / "teams" / "__templates__"
    )
    coord_dir = (
        tmp_path / ".treadmill" / "teams" / "__templates__" / "coordinator"
    )
    assert (coord_dir / "CLAUDE.md.tmpl").is_file()
    assert not (coord_dir / "AGENT.md").exists()


# ── ADR-0087 PR-H regressions — layout vs launcher cwd mismatch ────


def test_worker_settings_json_renders_under_dot_claude(
    synthetic_home: Path,
) -> None:
    """Regression for the 2026-06-10 layout mismatch: settings.json
    must land at <label>/.claude/settings.json, NOT <label>/settings.json,
    or Claude Code's project-settings discovery doesn't fire and the
    PostToolUse relay-inject hook never registers.

    See docs/learnings/2026-06-10-template-install-layout-vs-launcher-
    cwd-mismatch.md.
    """
    spec = make_team_spec("medicoder", worker_count=1)
    install_team(spec)
    worker_dir = (
        synthetic_home / ".treadmill" / "teams" / "medicoder"
        / "worker-medicoder-1"
    )
    assert (worker_dir / ".claude" / "settings.json").is_file(), (
        "settings.json must live under .claude/ for Claude Code discovery"
    )
    assert not (worker_dir / "settings.json").exists(), (
        "bare settings.json at cwd is the legacy bug shape — must not exist"
    )


def test_install_team_removes_stale_root_claude_md(
    synthetic_home: Path,
) -> None:
    """Pre-ADR-0087 deployments left a stale CLAUDE.md at the team-dir
    root (ADR-0084 era, ~23KB on existing medicoder team). Claude
    reads CLAUDE.md hierarchically so the stale parent shadows the
    new per-label files. install_team() must unlink it.
    """
    team_dir = synthetic_home / ".treadmill" / "teams" / "medicoder"
    team_dir.mkdir(parents=True, exist_ok=True)
    stale = team_dir / "CLAUDE.md"
    stale.write_text("# stale ADR-0084 content\n")

    install_team(make_team_spec("medicoder", worker_count=1))

    assert not stale.exists(), "stale parent CLAUDE.md must be removed"
    # Per-label CLAUDE.md files DO exist (the install renders them).
    assert (
        team_dir / "coordinator-medicoder" / "CLAUDE.md"
    ).is_file()
    assert (
        team_dir / "worker-medicoder-1" / "CLAUDE.md"
    ).is_file()


def test_install_team_root_claude_cleanup_idempotent(
    synthetic_home: Path,
) -> None:
    """Second `team up` (no stale file present) must not raise. The
    `if stale.exists()` guard makes the unlink idempotent."""
    spec = make_team_spec("medicoder", worker_count=1)
    install_team(spec)
    # No stale file remaining after first install. Second install must
    # complete cleanly.
    install_team(spec)
    assert not (
        synthetic_home / ".treadmill" / "teams" / "medicoder" / "CLAUDE.md"
    ).exists()


# ── settings.json.tmpl schema-validity (PR #295 regression) ──────────


# Top-level keys Claude Code's settings schema accepts that this
# template is allowed to use. Anything else (e.g. "$comment") raises an
# interactive "values were skipped — Continue?" prompt at session boot,
# which wedges an unattended worker until an operator presses Enter
# (both medicoder workers wedged on the first ADR-0087 team boot —
# 2026-06-10). Extend deliberately; never add documentation keys.
_SETTINGS_ALLOWED_TOP_LEVEL = {"permissions", "hooks", "env", "model"}


def test_settings_template_has_only_schema_keys() -> None:
    """The rendered worker settings.json must be pure schema. Unknown
    keys (at top level or inside permissions/hooks) trigger Claude
    Code's boot-time settings-validation prompt and wedge unattended
    sessions."""
    template = (
        Path(__file__).resolve().parent.parent
        / "worker" / "settings.json.tmpl"
    )
    body = json.loads(template.read_text())
    unknown = set(body) - _SETTINGS_ALLOWED_TOP_LEVEL
    assert not unknown, (
        f"non-schema top-level keys in settings.json.tmpl: {unknown} — "
        "these wedge unattended worker boots (see PR #295)"
    )
    # The two nested surfaces that wedged before: permissions and hooks
    # must not carry documentation keys either.
    assert set(body.get("permissions", {})) <= {"allow", "deny", "ask"}
    hooks = body.get("hooks", {})
    known_hook_events = {
        "PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop",
        "SubagentStop", "Notification", "PreCompact", "SessionStart",
        "SessionEnd",
    }
    assert set(hooks) <= known_hook_events, (
        f"unknown hook-event keys: {set(hooks) - known_hook_events}"
    )
