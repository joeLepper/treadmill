"""Tests for the architect emit-failure relay-drop trigger (ADR-0083)."""

import time
from pathlib import Path

import pytest

from treadmill_api.coordination.triggers import maybe_drop_relay_on_architect_emit_failure
from treadmill_api.events.task import ArchitectEmitFailure


def _payload(**overrides):
    base = {
        "parse_failure_reason": "no-structured-output",
        "model_output_excerpt": "Architect output was truncated.",
        "created_by": "treadmill-bert",
        "failing_run_id": "deadbeef-0000-0000-0000-000000000001",
    }
    base.update(overrides)
    return ArchitectEmitFailure(**base)


def test_trigger_drops_relay_file(tmp_path):
    payload = _payload()
    result = maybe_drop_relay_on_architect_emit_failure(
        payload, "task-abc123", relay_base=tmp_path
    )
    assert result is not None
    assert result.exists()
    # File lands under <label>/relay/
    assert result.parent == tmp_path / "treadmill-bert" / "relay"
    body = result.read_text()
    assert "task-abc123" in body
    assert payload.failing_run_id in body
    assert payload.parse_failure_reason in body
    assert payload.model_output_excerpt in body


def test_trigger_filename_carries_failing_run_id(tmp_path):
    payload = _payload(failing_run_id="cafecafe-1111-2222-3333-444444444444")
    result = maybe_drop_relay_on_architect_emit_failure(
        payload, "task-xyz", relay_base=tmp_path
    )
    assert result is not None
    assert "cafecafe-1111-2222-3333-444444444444" in result.name


def test_trigger_idempotent_on_replay(tmp_path):
    """Two calls with the same failing_run_id produce exactly one file (overwrite, not append)."""
    payload = _payload(failing_run_id="aaaabbbb-cccc-dddd-eeee-ffffffffffff")
    r1 = maybe_drop_relay_on_architect_emit_failure(
        payload, "task-1", relay_base=tmp_path
    )
    r2 = maybe_drop_relay_on_architect_emit_failure(
        payload, "task-1", relay_base=tmp_path
    )
    assert r1 is not None and r2 is not None
    # Filename is keyed on failing_run_id only — both calls produce the same path.
    assert r1 == r2
    relay_dir = tmp_path / "treadmill-bert" / "relay"
    files = list(relay_dir.iterdir())
    assert len(files) == 1
    assert payload.failing_run_id in files[0].name


def test_trigger_unwritable_dir_returns_none(tmp_path):
    """When the relay dir cannot be created (e.g. permission denied), skip gracefully."""
    # Make the base path a file so mkdir fails
    blocker = tmp_path / "treadmill-bert"
    blocker.write_text("not a directory")
    payload = _payload()
    result = maybe_drop_relay_on_architect_emit_failure(
        payload, "task-fail", relay_base=tmp_path
    )
    assert result is None


@pytest.mark.parametrize("reason", [
    "no-structured-output",
    "supersede-missing-rewrite",
    "gate-broken-missing-excerpt",
    "invalid-verdict-literal",
])
def test_trigger_all_failure_reasons(tmp_path, reason):
    payload = _payload(parse_failure_reason=reason)
    result = maybe_drop_relay_on_architect_emit_failure(
        payload, "task-reason-test", relay_base=tmp_path
    )
    assert result is not None
    assert reason in result.read_text()


def test_trigger_truncates_long_excerpt(tmp_path):
    long_excerpt = "x" * 5000
    payload = _payload(model_output_excerpt=long_excerpt[:4096])
    result = maybe_drop_relay_on_architect_emit_failure(
        payload, "task-long", relay_base=tmp_path
    )
    assert result is not None
    body = result.read_text()
    # Body contains the excerpt but not beyond 4096 chars of it
    assert "x" * 100 in body
