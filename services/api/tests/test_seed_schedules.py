"""Unit tests for ``treadmill_api.seed.schedules``.

Validates the SEED_SCHEDULES constant (field values, count, no duplicates)
and the two seeding paths: HTTP-driven (seed_schedules) and startup DB-direct
(seed_schedules_if_empty). Integration tests (live Postgres) are separate.

Validation per the ADR-0035 plan task ``seed-schedules``:
  ``cd services/api && uv run pytest tests/test_seed_schedules.py -q``
"""

from __future__ import annotations

from unittest.mock import MagicMock

from treadmill_api.seed.schedules import (
    SEED_SCHEDULES,
    ScheduleSeedError,
    seed_schedules,
    seed_schedules_if_empty,
)

_EXPECTED_WORKFLOW_IDS = {
    "wf-documentarian-audit",
    "wf-crystallize-learning",
    "wf-stuck-task-sweep",
    "wf-o11y-regression-scan",
}


# ── SEED_SCHEDULES content invariants ────────────────────────────────────────


def test_seed_schedules_has_four_entries() -> None:
    assert len(SEED_SCHEDULES) == 4


def test_seed_schedules_workflow_ids() -> None:
    ids = {s["workflow_id"] for s in SEED_SCHEDULES}
    assert ids == _EXPECTED_WORKFLOW_IDS


def test_no_duplicate_workflow_ids() -> None:
    ids = [s["workflow_id"] for s in SEED_SCHEDULES]
    assert len(ids) == len(set(ids)), f"duplicate workflow_ids: {ids}"


def test_seed_schedules_cron_expressions() -> None:
    by_wf = {s["workflow_id"]: s["cron_expression"] for s in SEED_SCHEDULES}
    assert by_wf["wf-documentarian-audit"] == "0 9 * * 1"
    assert by_wf["wf-crystallize-learning"] == "0 20 * * 0"
    assert by_wf["wf-stuck-task-sweep"] == "*/10 * * * *"
    assert by_wf["wf-o11y-regression-scan"] == "*/15 * * * *"


def test_seed_schedules_quiet_hours() -> None:
    by_wf = {s["workflow_id"]: s for s in SEED_SCHEDULES}
    assert by_wf["wf-documentarian-audit"]["quiet_hours"] == "20-6"
    assert by_wf["wf-documentarian-audit"]["quiet_tz"] == "America/Los_Angeles"
    assert by_wf["wf-crystallize-learning"]["quiet_hours"] is None
    assert by_wf["wf-stuck-task-sweep"]["quiet_hours"] is None
    assert by_wf["wf-o11y-regression-scan"]["quiet_hours"] is None


def test_seed_schedules_payload_templates() -> None:
    by_wf = {s["workflow_id"]: s["payload_template"] for s in SEED_SCHEDULES}
    assert by_wf["wf-documentarian-audit"] == {"trigger": "scheduled-audit"}
    assert by_wf["wf-crystallize-learning"] == {"trigger": "scheduled-sweep"}
    assert by_wf["wf-stuck-task-sweep"] == {"trigger": "scheduled-sweep"}
    assert by_wf["wf-o11y-regression-scan"] == {"trigger": "scheduled-scan"}


def test_all_entries_have_required_fields() -> None:
    for s in SEED_SCHEDULES:
        assert s.get("workflow_id"), f"missing workflow_id: {s}"
        assert s.get("cron_expression"), f"missing cron_expression: {s}"
        assert "payload_template" in s, f"missing payload_template: {s}"
        assert "quiet_hours" in s, f"missing quiet_hours key: {s}"
        assert s.get("quiet_tz"), f"missing quiet_tz: {s}"
        assert "jitter_seconds" in s, f"missing jitter_seconds: {s}"
        assert s.get("created_by"), f"missing created_by: {s}"


def test_all_entries_created_by_auto_seed() -> None:
    for s in SEED_SCHEDULES:
        assert s["created_by"] == "auto-seed", (
            f"expected created_by='auto-seed', got {s['created_by']!r} in {s}"
        )


def test_all_entries_have_valid_cron_five_fields() -> None:
    for s in SEED_SCHEDULES:
        fields = s["cron_expression"].split()
        assert len(fields) == 5, (
            f"{s['workflow_id']!r} cron_expression must be 5-field: "
            f"{s['cron_expression']!r}"
        )


# ── seed_schedules() — HTTP path ─────────────────────────────────────────────


def _fresh_client() -> MagicMock:
    """Mock API client returning an empty schedule list on GET."""
    client = MagicMock()
    client._request.side_effect = lambda method, path, **kw: (
        [] if method == "GET" else {"id": "00000000-0000-0000-0000-000000000001"}
    )
    return client


def _existing_client(existing: list[dict]) -> MagicMock:
    """Mock API client returning ``existing`` on GET."""
    client = MagicMock()
    client._request.side_effect = lambda method, path, **kw: (
        existing if method == "GET" else {"id": "00000000-0000-0000-0000-000000000001"}
    )
    return client


def test_seed_schedules_creates_all_four_on_fresh_install() -> None:
    created = seed_schedules(_fresh_client())
    assert created == 4


def test_seed_schedules_idempotent_when_all_exist() -> None:
    existing = [
        {"workflow_id": s["workflow_id"], "cron_expression": s["cron_expression"]}
        for s in SEED_SCHEDULES
    ]
    created = seed_schedules(_existing_client(existing))
    assert created == 0


def test_seed_schedules_no_posts_when_all_exist() -> None:
    existing = [
        {"workflow_id": s["workflow_id"], "cron_expression": s["cron_expression"]}
        for s in SEED_SCHEDULES
    ]
    client = _existing_client(existing)
    seed_schedules(client)
    post_calls = [c for c in client._request.call_args_list if c.args[0] == "POST"]
    assert post_calls == []


def test_seed_schedules_only_posts_missing() -> None:
    """When one schedule already exists, only the other three are POSTed."""
    existing = [{"workflow_id": "wf-documentarian-audit", "cron_expression": "0 9 * * 1"}]
    client = _existing_client(existing)
    created = seed_schedules(client)
    assert created == 3
    post_calls = [c for c in client._request.call_args_list if c.args[0] == "POST"]
    posted_wf_ids = {c.kwargs["json"]["workflow_id"] for c in post_calls}
    assert "wf-documentarian-audit" not in posted_wf_ids
    assert len(posted_wf_ids) == 3


def test_seed_schedules_posts_all_four_workflow_ids() -> None:
    client = _fresh_client()
    seed_schedules(client)
    post_calls = [c for c in client._request.call_args_list if c.args[0] == "POST"]
    posted_wf_ids = {c.kwargs["json"]["workflow_id"] for c in post_calls}
    assert posted_wf_ids == _EXPECTED_WORKFLOW_IDS


def test_seed_schedules_documentarian_quiet_hours_posted() -> None:
    client = _fresh_client()
    seed_schedules(client)
    post_calls = [c for c in client._request.call_args_list if c.args[0] == "POST"]
    doc_call = next(
        c for c in post_calls
        if c.kwargs["json"]["workflow_id"] == "wf-documentarian-audit"
    )
    assert doc_call.kwargs["json"]["quiet_hours"] == "20-6"
    assert doc_call.kwargs["json"]["quiet_tz"] == "America/Los_Angeles"


def test_seed_schedules_ops_bots_post_no_quiet_hours() -> None:
    """Stuck-task-sweep and o11y-regression-scan post with quiet_hours=None."""
    client = _fresh_client()
    seed_schedules(client)
    post_calls = [c for c in client._request.call_args_list if c.args[0] == "POST"]
    for wf_id in ("wf-stuck-task-sweep", "wf-o11y-regression-scan"):
        call = next(
            c for c in post_calls if c.kwargs["json"]["workflow_id"] == wf_id
        )
        assert call.kwargs["json"]["quiet_hours"] is None, (
            f"{wf_id} should have quiet_hours=None"
        )


def test_seed_schedules_posts_correct_payload_templates() -> None:
    client = _fresh_client()
    seed_schedules(client)
    post_calls = [c for c in client._request.call_args_list if c.args[0] == "POST"]
    by_wf = {
        c.kwargs["json"]["workflow_id"]: c.kwargs["json"]["payload_template"]
        for c in post_calls
    }
    assert by_wf["wf-documentarian-audit"] == {"trigger": "scheduled-audit"}
    assert by_wf["wf-crystallize-learning"] == {"trigger": "scheduled-sweep"}
    assert by_wf["wf-stuck-task-sweep"] == {"trigger": "scheduled-sweep"}
    assert by_wf["wf-o11y-regression-scan"] == {"trigger": "scheduled-scan"}


def test_seed_schedules_posts_correct_cron_expressions() -> None:
    client = _fresh_client()
    seed_schedules(client)
    post_calls = [c for c in client._request.call_args_list if c.args[0] == "POST"]
    by_wf = {
        c.kwargs["json"]["workflow_id"]: c.kwargs["json"]["cron_expression"]
        for c in post_calls
    }
    assert by_wf["wf-documentarian-audit"] == "0 9 * * 1"
    assert by_wf["wf-crystallize-learning"] == "0 20 * * 0"
    assert by_wf["wf-stuck-task-sweep"] == "*/10 * * * *"
    assert by_wf["wf-o11y-regression-scan"] == "*/15 * * * *"


# ── seed_schedules_if_empty() — DB path ──────────────────────────────────────


def _make_session(existing_count: int = 0) -> MagicMock:
    """Mock session whose execute().scalar_one() returns ``existing_count``."""
    session = MagicMock()
    session.execute.return_value.scalar_one.return_value = existing_count
    return session


def test_seed_schedules_if_empty_skips_when_rows_exist() -> None:
    session = _make_session(existing_count=2)
    result = seed_schedules_if_empty(session)
    assert result == 0
    session.add.assert_not_called()
    session.commit.assert_not_called()


def test_seed_schedules_if_empty_inserts_four_on_fresh_db() -> None:
    session = _make_session(existing_count=0)
    result = seed_schedules_if_empty(session)
    assert result == 4
    assert session.add.call_count == 4
    session.commit.assert_called_once()


def test_seed_schedules_if_empty_adds_schedule_instances() -> None:
    from treadmill_api.models.schedule import Schedule

    session = _make_session(existing_count=0)
    seed_schedules_if_empty(session)
    added = [c.args[0] for c in session.add.call_args_list]
    assert all(isinstance(s, Schedule) for s in added)


def test_seed_schedules_if_empty_all_active() -> None:
    """All seeded schedules must be inserted with status='active' (enabled)."""
    session = _make_session(existing_count=0)
    seed_schedules_if_empty(session)
    added = [c.args[0] for c in session.add.call_args_list]
    assert all(s.status == "active" for s in added), (
        "all seeded schedules should start as active (enabled)"
    )


def test_seed_schedules_if_empty_correct_workflow_ids() -> None:
    session = _make_session(existing_count=0)
    seed_schedules_if_empty(session)
    added_wf_ids = {c.args[0].workflow_id for c in session.add.call_args_list}
    assert added_wf_ids == _EXPECTED_WORKFLOW_IDS


def test_seed_schedules_if_empty_correct_cron_expressions() -> None:
    session = _make_session(existing_count=0)
    seed_schedules_if_empty(session)
    added = {c.args[0].workflow_id: c.args[0].cron_expression
             for c in session.add.call_args_list}
    assert added["wf-documentarian-audit"] == "0 9 * * 1"
    assert added["wf-crystallize-learning"] == "0 20 * * 0"
    assert added["wf-stuck-task-sweep"] == "*/10 * * * *"
    assert added["wf-o11y-regression-scan"] == "*/15 * * * *"


def test_seed_schedules_if_empty_documentarian_quiet_hours() -> None:
    session = _make_session(existing_count=0)
    seed_schedules_if_empty(session)
    added = {c.args[0].workflow_id: c.args[0] for c in session.add.call_args_list}
    doc = added["wf-documentarian-audit"]
    assert doc.quiet_hours == "20-6"
    assert doc.quiet_tz == "America/Los_Angeles"


def test_seed_schedules_if_empty_ops_bots_no_quiet_hours() -> None:
    session = _make_session(existing_count=0)
    seed_schedules_if_empty(session)
    added = {c.args[0].workflow_id: c.args[0] for c in session.add.call_args_list}
    assert added["wf-stuck-task-sweep"].quiet_hours is None
    assert added["wf-o11y-regression-scan"].quiet_hours is None
