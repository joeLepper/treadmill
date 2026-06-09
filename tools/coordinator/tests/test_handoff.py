"""Smoke + structure tests for tools/coordinator/handoff.py."""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_HANDOFF_PATH = Path(__file__).resolve().parents[1] / "handoff.py"
_spec = importlib.util.spec_from_file_location("handoff", _HANDOFF_PATH)
handoff = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["handoff"] = handoff
_spec.loader.exec_module(handoff)  # type: ignore[union-attr]


# ── Smoke targets ──────────────────────────────────────────────────────────


def test_import_succeeds() -> None:
    assert handoff.main is not None
    assert handoff.build_handoff is not None
    assert handoff.fetch_task_board is not None


def test_help_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, str(_HANDOFF_PATH), "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert "--plan-id" in result.stdout
    assert "--output-dir" in result.stdout
    assert "--api-url" in result.stdout


# ── build_handoff() pure-function tests ────────────────────────────────────


def test_build_handoff_empty_board_still_renders() -> None:
    out = handoff.build_handoff(
        plan_id="p-1",
        rows=[],
        timestamp="2026-06-08T22:00:00Z",
        api_url="http://localhost:8088",
    )
    assert "# Coordinator handoff" in out
    assert "p-1" in out
    assert "No tasks on the board for this plan" in out
    assert "No tasks currently blocked" in out


def test_build_handoff_renders_snapshot_table() -> None:
    rows = [
        {
            "task_id": "t-1", "assignee": "treadmill-bert",
            "status": "in_flight", "branch": "feat/t1", "pr_number": 100,
            "updated_at": "2026-06-08T21:00:00Z",
        },
        {
            "task_id": "t-2", "assignee": "treadmill-donna",
            "status": "waiting_review", "branch": "feat/t2", "pr_number": 101,
            "updated_at": "2026-06-08T21:30:00Z",
        },
    ]
    out = handoff.build_handoff(
        plan_id="p-1", rows=rows,
        timestamp="2026-06-08T22:00:00Z",
        api_url="http://localhost:8088",
    )
    assert "| task_id |" in out
    assert "`t-1`" in out
    assert "`t-2`" in out
    assert "treadmill-bert" in out
    assert "in_flight" in out
    assert "waiting_review" in out


def test_build_handoff_lane_summary_groups_by_assignee() -> None:
    rows = [
        {"task_id": "t-1", "assignee": "treadmill-bert", "status": "in_flight",
         "updated_at": "2026-06-08T21:00:00Z"},
        {"task_id": "t-2", "assignee": "treadmill-bert", "status": "done",
         "updated_at": "2026-06-08T20:00:00Z"},
        {"task_id": "t-3", "assignee": "treadmill-donna", "status": "in_flight",
         "updated_at": "2026-06-08T21:30:00Z"},
    ]
    out = handoff.build_handoff(
        plan_id="p-1", rows=rows,
        timestamp="2026-06-08T22:00:00Z",
        api_url="http://localhost:8088",
    )
    assert "### `treadmill-bert`" in out
    assert "### `treadmill-donna`" in out
    # bert has 2 tasks, donna has 1
    assert "2 task(s)" in out
    assert "1 task(s)" in out
    # Status counts visible
    assert "1 in_flight" in out
    assert "1 done" in out


def test_build_handoff_unresolved_signals_for_blocked_states() -> None:
    rows = [
        {
            "task_id": "t-1", "assignee": "treadmill-bert",
            "status": "blocked_operator",
            "notes": "waiting on Joe to approve scope change",
        },
        {
            "task_id": "t-2", "assignee": "treadmill-donna",
            "status": "blocked_dependency",
            "notes": "blocked on t-1 merge",
        },
        {
            "task_id": "t-3", "assignee": "treadmill-bert",
            "status": "in_flight", "notes": None,
        },
    ]
    out = handoff.build_handoff(
        plan_id="p-1", rows=rows,
        timestamp="2026-06-08T22:00:00Z",
        api_url="http://localhost:8088",
    )
    # Both blocked rows surface in the unresolved section
    assert "blocked_operator (1)" in out
    assert "blocked_dependency (1)" in out
    assert "waiting on Joe to approve" in out
    assert "blocked on t-1 merge" in out
    # The in_flight row does NOT bleed into unresolved
    unresolved_idx = out.index("## Unresolved signals")
    next_section_idx = out.index("## Recommended next actions")
    unresolved_block = out[unresolved_idx:next_section_idx]
    assert "t-3" not in unresolved_block


def test_build_handoff_operator_instance_when_set() -> None:
    out = handoff.build_handoff(
        plan_id="p-1", rows=[],
        timestamp="2026-06-08T22:00:00Z",
        api_url="http://localhost:8088",
        operator_instance="treadmill-alan",
    )
    assert "`treadmill-alan`" in out
    assert "Not recorded in this handoff" not in out


def test_build_handoff_operator_instance_placeholder_when_unset() -> None:
    out = handoff.build_handoff(
        plan_id="p-1", rows=[],
        timestamp="2026-06-08T22:00:00Z",
        api_url="http://localhost:8088",
    )
    assert "Not recorded in this handoff" in out


def test_build_handoff_unassigned_row_handled() -> None:
    """Rows with no assignee group under '_unassigned_' rather than crashing."""
    rows = [
        {"task_id": "t-1", "assignee": None, "status": "ready",
         "updated_at": "2026-06-08T21:00:00Z"},
    ]
    out = handoff.build_handoff(
        plan_id="p-1", rows=rows,
        timestamp="2026-06-08T22:00:00Z",
        api_url="http://localhost:8088",
    )
    assert "_unassigned_" in out


# ── fetch_task_board() API shape tolerance ─────────────────────────────────


def test_fetch_accepts_list_response() -> None:
    """Bare-list response payload (the simplest API shape)."""
    fake_resp_body = json.dumps([
        {"task_id": "t-1", "status": "ready", "assignee": None},
    ])
    with patch("handoff.urllib.request.urlopen") as mock_open:
        ctx = mock_open.return_value.__enter__.return_value
        ctx.read.return_value = fake_resp_body.encode("utf-8")
        rows = handoff.fetch_task_board(
            api_url="http://localhost:8088", plan_id="p-1",
        )
    assert rows == [{"task_id": "t-1", "status": "ready", "assignee": None}]


def test_fetch_accepts_envelope_response() -> None:
    """`{rows: [...]}` envelope response (common alternative shape)."""
    fake_resp_body = json.dumps({"rows": [
        {"task_id": "t-1", "status": "ready"},
    ]})
    with patch("handoff.urllib.request.urlopen") as mock_open:
        ctx = mock_open.return_value.__enter__.return_value
        ctx.read.return_value = fake_resp_body.encode("utf-8")
        rows = handoff.fetch_task_board(
            api_url="http://localhost:8088", plan_id="p-1",
        )
    assert rows == [{"task_id": "t-1", "status": "ready"}]


def test_fetch_adds_bearer_when_api_key_given() -> None:
    fake_resp_body = json.dumps([])
    with patch("handoff.urllib.request.urlopen") as mock_open:
        ctx = mock_open.return_value.__enter__.return_value
        ctx.read.return_value = fake_resp_body.encode("utf-8")
        handoff.fetch_task_board(
            api_url="http://localhost:8088", plan_id="p-1", api_key="secret",
        )
    sent_req = mock_open.call_args[0][0]
    assert sent_req.get_header("Authorization") == "Bearer secret"


def test_fetch_rejects_malformed_shape() -> None:
    fake_resp_body = json.dumps({"unexpected": "shape"})
    with patch("handoff.urllib.request.urlopen") as mock_open:
        ctx = mock_open.return_value.__enter__.return_value
        ctx.read.return_value = fake_resp_body.encode("utf-8")
        with pytest.raises(ValueError):
            handoff.fetch_task_board(
                api_url="http://localhost:8088", plan_id="p-1",
            )


# ── main() integration with mocked fetch ───────────────────────────────────


def test_main_writes_file_and_prints_path(tmp_path: Path, monkeypatch) -> None:
    rows = [
        {"task_id": "t-1", "assignee": "treadmill-bert", "status": "in_flight",
         "branch": "feat/t1", "pr_number": 100,
         "updated_at": "2026-06-08T21:00:00Z"},
    ]
    monkeypatch.setattr(handoff, "fetch_task_board", lambda **kwargs: rows)
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)

    exit_code = handoff.main([
        "--plan-id", "p-1",
        "--output-dir", str(tmp_path),
        "--timestamp-override", "2026-06-08T22:00:00Z",
    ])

    assert exit_code == 0
    printed_path = captured.getvalue().strip()
    assert printed_path.endswith(".md")
    out_file = Path(printed_path)
    assert out_file.exists()
    content = out_file.read_text()
    assert "# Coordinator handoff — plan `p-1`" in content
    assert "`t-1`" in content


def test_main_falls_back_to_env_plan_id(tmp_path: Path, monkeypatch) -> None:
    """When --plan-id is omitted, the first id in TREADMILL_COORDINATOR_PLANS
    is used."""
    monkeypatch.setattr(handoff, "fetch_task_board", lambda **kwargs: [])
    monkeypatch.setenv(
        "TREADMILL_COORDINATOR_PLANS", "p-from-env,p-second",
    )
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)

    exit_code = handoff.main([
        "--output-dir", str(tmp_path),
        "--timestamp-override", "2026-06-08T22:00:00Z",
    ])

    assert exit_code == 0
    out_file = Path(captured.getvalue().strip())
    assert "p-from-env" in out_file.read_text()


def test_main_errors_without_plan_id() -> None:
    """No --plan-id and no env fallback → non-zero exit, helpful message."""
    result = subprocess.run(
        [sys.executable, str(_HANDOFF_PATH)],
        env={"PATH": ""},  # strip env; remove TREADMILL_COORDINATOR_PLANS
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "--plan-id" in result.stderr


def test_main_includes_operator_instance_from_env(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(handoff, "fetch_task_board", lambda **kwargs: [])
    monkeypatch.setenv("TREADMILL_OPERATOR_INSTANCE", "treadmill-alan")
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)

    handoff.main([
        "--plan-id", "p-1",
        "--output-dir", str(tmp_path),
        "--timestamp-override", "2026-06-08T22:00:00Z",
    ])

    out_file = Path(captured.getvalue().strip())
    assert "`treadmill-alan`" in out_file.read_text()
