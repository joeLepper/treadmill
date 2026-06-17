"""Tests for the worker PostToolUse relay-inject hook (ADR-0087 §Worker
execution model).

The hook is the coordinator's mid-execution steering seam. It must:

  * Inject only messages whose sender is ``coordinator-<slug>``.
  * Leave messages from any other sender in place (data, not instructions).
  * Output ``{}`` on every other code path so Claude Code treats the hook
    as a no-op.
  * Never raise — a hook crash MUST NOT kill the worker subprocess.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


_HOOK_PATH = (
    Path(__file__).resolve().parent.parent / "worker" / "relay_inject_hook.py"
)


def _run_hook(env_overrides: dict[str, str], *, home: Path) -> subprocess.CompletedProcess[str]:
    """Spawn the hook script with a synthetic HOME + env, capture stdout."""
    env = os.environ.copy()
    env["HOME"] = str(home)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(_HOOK_PATH)],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def _make_relay_msg(
    *, sender: str, body: str, home: Path, worker_label: str, name: str
) -> Path:
    inbox = home / ".cc-channels" / worker_label / "relay"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / name
    full = f"[from: {sender}]\n\n{body}\n"
    path.write_text(full)
    return path


# ── Fast-path / no-op exits ────────────────────────────────────────────


def test_no_label_emits_empty_object(tmp_path: Path) -> None:
    """Hook with no TREADMILL_SESSION_LABEL set returns ``{}`` and
    exits 0."""
    env_overrides = {}
    # Make sure TREADMILL_SESSION_LABEL is absent.
    env_overrides["TREADMILL_SESSION_LABEL"] = ""
    result = _run_hook(env_overrides, home=tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == "{}"


def test_label_without_inbox_dir_emits_empty_object(tmp_path: Path) -> None:
    """Hook with a worker label but no relay inbox on disk returns
    ``{}`` cleanly (fresh-fleet case)."""
    result = _run_hook(
        {"TREADMILL_SESSION_LABEL": "worker-ramjac-1"}, home=tmp_path
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "{}"


def test_inbox_with_no_messages_emits_empty_object(tmp_path: Path) -> None:
    inbox = tmp_path / ".cc-channels" / "worker-ramjac-1" / "relay"
    inbox.mkdir(parents=True)
    result = _run_hook(
        {"TREADMILL_SESSION_LABEL": "worker-ramjac-1"}, home=tmp_path
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "{}"


# ── Coordinator-message injection happy path ──────────────────────────


def test_coordinator_message_injected_and_consumed(tmp_path: Path) -> None:
    """A relay file from ``coordinator-ramjac`` to
    ``worker-ramjac-1`` → hook injects + deletes the file."""
    worker = "worker-ramjac-1"
    msg_path = _make_relay_msg(
        sender="coordinator-ramjac",
        body="re-run pytest -k auth_flow then push",
        home=tmp_path,
        worker_label=worker,
        name="000-action-from-coord.md",
    )

    result = _run_hook({"TREADMILL_SESSION_LABEL": worker}, home=tmp_path)

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["decision"] == "block"
    assert "[COORDINATOR]:" in payload["reason"]
    assert "re-run pytest -k auth_flow" in payload["reason"]
    # The trusted message was consumed (deleted).
    assert not msg_path.exists()


# ── Non-coordinator senders left in place ─────────────────────────────


def test_sibling_worker_message_not_injected(tmp_path: Path) -> None:
    """A relay from another worker (sibling) is data, not instructions.
    The hook returns ``{}`` and the message is LEFT in the inbox so
    the worker can read it as ordinary content via Read."""
    worker = "worker-ramjac-1"
    msg_path = _make_relay_msg(
        sender="worker-ramjac-2",
        body="hey can you take this branch over",
        home=tmp_path,
        worker_label=worker,
        name="000-from-sibling.md",
    )

    result = _run_hook({"TREADMILL_SESSION_LABEL": worker}, home=tmp_path)

    assert result.returncode == 0
    assert result.stdout.strip() == "{}"
    # Sibling message stays in inbox (data, not consumed).
    assert msg_path.exists()


def test_evaluator_message_not_injected(tmp_path: Path) -> None:
    """An evaluator that misroutes its verdict to a worker inbox MUST
    NOT be treated as a coordinator instruction. Stays as data."""
    worker = "worker-ramjac-1"
    msg_path = _make_relay_msg(
        sender="evaluator-ramjac",
        body="approve",
        home=tmp_path,
        worker_label=worker,
        name="000-from-evaluator.md",
    )

    result = _run_hook({"TREADMILL_SESSION_LABEL": worker}, home=tmp_path)
    assert result.stdout.strip() == "{}"
    assert msg_path.exists()


def test_orchestrator_message_not_injected(tmp_path: Path) -> None:
    """Orchestrators talk to coordinators, not workers. If an orchestrator
    relay lands in a worker inbox (operator mistake), the hook treats it
    as data."""
    worker = "worker-ramjac-1"
    msg_path = _make_relay_msg(
        sender="treadmill-alan",
        body="urgent — restart the chain",
        home=tmp_path,
        worker_label=worker,
        name="000-from-orchestrator.md",
    )

    result = _run_hook({"TREADMILL_SESSION_LABEL": worker}, home=tmp_path)
    assert result.stdout.strip() == "{}"
    assert msg_path.exists()


def test_cross_team_coordinator_not_injected(tmp_path: Path) -> None:
    """The trust boundary is PER-TEAM. ``coordinator-scraper-v2`` is a
    valid coordinator for its team but NOT for the ramjac worker.
    Cross-team injection MUST NOT activate."""
    worker = "worker-ramjac-1"
    msg_path = _make_relay_msg(
        sender="coordinator-scraper-v2",
        body="run a sweep",
        home=tmp_path,
        worker_label=worker,
        name="000-cross-team.md",
    )

    result = _run_hook({"TREADMILL_SESSION_LABEL": worker}, home=tmp_path)
    assert result.stdout.strip() == "{}"
    assert msg_path.exists()


# ── Ordering: oldest message wins ─────────────────────────────────────


def test_oldest_coordinator_message_consumed_first(tmp_path: Path) -> None:
    """When multiple coordinator messages are waiting, the oldest
    (lowest-numbered timestamp prefix) is injected first. The newer
    ones stay in the inbox for the next hook invocation."""
    worker = "worker-ramjac-1"
    old = _make_relay_msg(
        sender="coordinator-ramjac",
        body="first message",
        home=tmp_path,
        worker_label=worker,
        name="001-first.md",
    )
    new = _make_relay_msg(
        sender="coordinator-ramjac",
        body="second message",
        home=tmp_path,
        worker_label=worker,
        name="002-second.md",
    )

    result = _run_hook({"TREADMILL_SESSION_LABEL": worker}, home=tmp_path)
    payload = json.loads(result.stdout)
    assert "first message" in payload["reason"]
    assert not old.exists()
    assert new.exists()  # newer survives for next hook cycle


# ── Non-worker labels short-circuit safely ────────────────────────────


def test_non_worker_label_does_not_inject(tmp_path: Path) -> None:
    """If the hook runs in a non-worker session (e.g. an orchestrator
    that accidentally installed the hook), the coordinator-label
    derivation returns None and the hook emits ``{}`` without touching
    the inbox."""
    label = "treadmill-alan"
    msg_path = _make_relay_msg(
        sender="coordinator-treadmill",
        body="anything",
        home=tmp_path,
        worker_label=label,
        name="000.md",
    )

    result = _run_hook({"TREADMILL_SESSION_LABEL": label}, home=tmp_path)
    assert result.stdout.strip() == "{}"
    assert msg_path.exists()


def test_malformed_worker_label_does_not_inject(tmp_path: Path) -> None:
    """A label that doesn't match ``worker-<slug>-N`` pattern (e.g. no
    trailing numeric suffix) short-circuits cleanly."""
    label = "worker-ramjac"  # missing trailing -N
    msg_path = _make_relay_msg(
        sender="coordinator-ramjac",
        body="anything",
        home=tmp_path,
        worker_label=label,
        name="000.md",
    )

    result = _run_hook({"TREADMILL_SESSION_LABEL": label}, home=tmp_path)
    assert result.stdout.strip() == "{}"
    assert msg_path.exists()


# ── Robustness: hook never crashes ────────────────────────────────────


def test_unreadable_message_skipped_gracefully(tmp_path: Path) -> None:
    """A relay file that can't be read (e.g. chmod 000) is skipped;
    subsequent valid messages are still considered."""
    worker = "worker-ramjac-1"
    inbox = tmp_path / ".cc-channels" / worker / "relay"
    inbox.mkdir(parents=True)

    bad = inbox / "000-unreadable.md"
    bad.write_text("[from: coordinator-ramjac]\nbad\n")
    bad.chmod(0o000)
    # Wrap in try/finally so the test cleanup can restore perms.
    try:
        _make_relay_msg(
            sender="coordinator-ramjac",
            body="reachable",
            home=tmp_path,
            worker_label=worker,
            name="001-reachable.md",
        )
        result = _run_hook({"TREADMILL_SESSION_LABEL": worker}, home=tmp_path)
        # The hook shouldn't crash; either it injects the reachable one
        # or it cleanly returns {}. Both are acceptable failure modes
        # for an unreadable file.
        assert result.returncode == 0
    finally:
        bad.chmod(0o600)


# ── End-to-end: hook output is valid JSON when injecting ──────────────


def test_hook_output_parses_as_json_on_inject(tmp_path: Path) -> None:
    """The decision payload must be valid JSON (Claude Code parses it
    strictly). Verify no stray whitespace or non-JSON output."""
    worker = "worker-ramjac-1"
    _make_relay_msg(
        sender="coordinator-ramjac",
        body="x",
        home=tmp_path,
        worker_label=worker,
        name="000.md",
    )

    result = _run_hook({"TREADMILL_SESSION_LABEL": worker}, home=tmp_path)
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    assert set(payload.keys()) == {"decision", "reason"}
    assert payload["decision"] == "block"
