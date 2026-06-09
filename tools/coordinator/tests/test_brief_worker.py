"""Smoke + structure tests for tools/coordinator/brief_worker.py."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

# Load brief_worker.py by path (it lives outside the import tree).
_BRIEF_WORKER_PATH = Path(__file__).resolve().parents[1] / "brief_worker.py"
_spec = importlib.util.spec_from_file_location("brief_worker", _BRIEF_WORKER_PATH)
brief_worker = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["brief_worker"] = brief_worker
_spec.loader.exec_module(brief_worker)  # type: ignore[union-attr]


def test_import_succeeds() -> None:
    """The module loads (smoke target #1)."""
    assert brief_worker.main is not None


def test_help_exits_zero() -> None:
    """`python3 brief_worker.py --help` returns 0 (smoke target #2)."""
    result = subprocess.run(
        [sys.executable, str(_BRIEF_WORKER_PATH), "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert "--plan-id" in result.stdout
    assert "--task-id" in result.stdout
    assert "--worker" in result.stdout


def test_required_ids_only_emits_placeholder_brief(tmp_path: Path) -> None:
    """With only --plan-id + --task-id the brief still emits — placeholders
    surface what the coordinator still needs to fill in."""
    result = subprocess.run(
        [
            sys.executable, str(_BRIEF_WORKER_PATH),
            "--plan-id", "p-abc",
            "--task-id", "t-123",
            "--team-dir", str(tmp_path),
        ],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert "# Task brief — `t-123`" in result.stdout
    assert "**Plan**: `p-abc`" in result.stdout
    # Placeholders flag the gaps loudly so the coordinator notices.
    assert "_TODO" in result.stdout


def test_full_input_set_emits_fields(tmp_path: Path) -> None:
    """Supplying every CLI flag produces a brief with every field filled
    and no placeholder TODOs."""
    result = subprocess.run(
        [
            sys.executable, str(_BRIEF_WORKER_PATH),
            "--plan-id", "p-abc",
            "--task-id", "t-123",
            "--worker", "treadmill-bert",
            "--team-dir", str(tmp_path),
            "--task-intent", "Add the foo router to wire bar into baz.",
            "--task-scope", "routes/foo.js,routes/foo.test.js,routes/AGENT.md",
            "--active-peers", "treadmill-carla,treadmill-donna",
            "--related-adr", "0084",
            "--gates", "ci-green,review-approved",
        ],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    out = result.stdout
    assert "treadmill-bert" in out
    assert "`routes/foo.js`" in out
    assert "`routes/AGENT.md`" in out
    assert "Add the foo router" in out
    assert "ADR-0084" in out
    assert "treadmill-carla,treadmill-donna" in out
    assert "ci-green" in out
    assert "_TODO" not in out


def test_pitfalls_pulled_from_memory(tmp_path: Path) -> None:
    """When `<team-dir>/memory/main.md` has a `## Pitfalls` section, its
    `### YYYY-MM-DD ...` entries appear in the brief."""
    memory = tmp_path / "memory" / "main.md"
    memory.parent.mkdir(parents=True)
    memory.write_text(
        "# Per-repo memory for test-repo\n"
        "\n"
        "## Conventions\n"
        "Some convention.\n"
        "\n"
        "## Pitfalls\n"
        "### 2026-06-08 do not mock the database in integration tests\n"
        "**Why:** prior incident etc.\n"
        "\n"
        "### 2026-06-07 pipefail + command-substitution silent exit\n"
        "**Why:** caught after PR #186.\n"
        "\n"
        "## Prior plan summaries\n"
        "### 2026-06-01-foo — outcome\n"
        "Notes.\n"
    )
    result = subprocess.run(
        [
            sys.executable, str(_BRIEF_WORKER_PATH),
            "--plan-id", "p-1",
            "--task-id", "t-1",
            "--team-dir", str(tmp_path),
        ],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    out = result.stdout
    assert "do not mock the database" in out
    assert "pipefail + command-substitution" in out
    # The prior-plan-summary heading must NOT bleed in (we stop at the next ##).
    assert "outcome" not in out


def test_no_memory_file_says_so(tmp_path: Path) -> None:
    """Absent memory file → 'no pitfalls recorded yet' note in the brief."""
    result = subprocess.run(
        [
            sys.executable, str(_BRIEF_WORKER_PATH),
            "--plan-id", "p-1",
            "--task-id", "t-1",
            "--team-dir", str(tmp_path),
        ],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert "No pitfalls recorded yet" in result.stdout


def test_default_gates_present_when_unset(tmp_path: Path) -> None:
    """Without --gates, the default ADR-0030 + plan-skill gate set appears.
    These are the rules that bounce PRs to feedback when missed; every
    brief carries them by default."""
    result = subprocess.run(
        [
            sys.executable, str(_BRIEF_WORKER_PATH),
            "--plan-id", "p-1",
            "--task-id", "t-1",
            "--team-dir", str(tmp_path),
        ],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    out = result.stdout
    assert "docs-currency" in out
    assert "existing tests" in out
    assert "deterministic validation" in out


def test_missing_required_arg_errors() -> None:
    """argparse rejects missing --plan-id (or --task-id) with a non-zero
    exit; surfaces to the coordinator as a clear failure."""
    result = subprocess.run(
        [sys.executable, str(_BRIEF_WORKER_PATH), "--task-id", "t-1"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "--plan-id" in result.stderr


def test_build_brief_pure_function() -> None:
    """The build_brief() function is importable and pure — no FS, no IO.
    Coordinator-side tests can call it directly without subprocess."""
    out = brief_worker.build_brief(
        plan_id="p-1",
        task_id="t-1",
        worker="treadmill-bert",
        task_intent="Do the thing.",
        task_scope=["a.py", "b.py"],
        active_peers=["treadmill-carla"],
        pitfalls=["2026-06-08 do not foo when baring"],
        related_adr="0084",
        gates=["ci"],
    )
    assert "Do the thing." in out
    assert "treadmill-bert" in out
    assert "treadmill-carla" in out
    assert "do not foo when baring" in out
    assert "ADR-0084" in out
    assert "_TODO" not in out


def test_pitfalls_limit_default_five(tmp_path: Path) -> None:
    """The pitfalls list is capped at 5 entries so briefs don't bloat as
    memory accumulates; older pitfalls live in the file for reference."""
    memory = tmp_path / "memory" / "main.md"
    memory.parent.mkdir(parents=True)
    lines = ["## Pitfalls"]
    for i in range(10):
        lines.append(f"### 2026-06-0{i % 9 + 1} pitfall number {i}")
        lines.append(f"**Why:** reason {i}")
        lines.append("")
    memory.write_text("\n".join(lines))
    result = brief_worker._read_pitfalls(tmp_path)
    assert len(result) == 5
