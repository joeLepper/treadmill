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
        settings = worker_dir / "settings.json"
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


def test_install_team_does_not_render_coordinator_claude_md(
    synthetic_home: Path,
) -> None:
    """PR-D (Bert) owns coordinator CLAUDE.md content; this installer
    must not touch the coordinator's session dir. Stays explicit so a
    future contributor doesn't add a 'render coordinator template too'
    line without realizing PR-D owns it."""
    spec = make_team_spec("medicoder", worker_count=2)
    install_team(spec)

    coord_dir = (
        synthetic_home
        / ".treadmill"
        / "teams"
        / "medicoder"
        / "coordinator-medicoder"
    )
    assert not coord_dir.exists()
