"""Integration tests for the plan_status VIEW.

Fixture-driven, mirroring the task_status pattern. Each test seeds a
plan plus a sequence of ``plan.*`` events and asserts the resolved
``derived_status``. Covers every transition in the state machine
(per ADR-0010):

    drafting → planning → active → completed | abandoned

Plus the additional gate from the 2026-05-11 closure plan (D.4): the
priority order ``abandoned > completed > active > planning > drafting``
holds at v0 because the underlying state machine is monotonic except
for the abandoned-anytime transition; "last event wins" matches the
priority order under that constraint.

Skipped by default. To run:

  treadmill-local up
  TREADMILL_INTEGRATION=1 uv run pytest services/api/tests/test_integration_plan_status.py
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
TEST_DB_URL = os.environ.get("TREADMILL_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not (INTEGRATION and TEST_DB_URL),
    reason="set TREADMILL_INTEGRATION=1 and TREADMILL_TEST_DATABASE_URL (a DEDICATED test database) to run; requires `treadmill-local up`",
)


DEFAULT_API_URL = "http://localhost:8088"


@pytest.fixture(scope="module")
def database_url() -> str:
    return TEST_DB_URL


@pytest.fixture(scope="module")
def api_url() -> str:
    return os.environ.get("TREADMILL_API_URL", DEFAULT_API_URL)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Engine:
    eng = sa.create_engine(database_url, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module", autouse=True)
def migrations_applied(database_url: str) -> None:
    services_api_dir = Path(__file__).resolve().parent.parent
    env = {**os.environ, "DATABASE_URL": database_url}
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=services_api_dir,
        env=env,
        check=True,
    )


_TEST_TABLES = ("plans", "events")


@pytest.fixture
def fixtures(engine: Engine) -> Iterator["PlanFixtureBuilder"]:
    builder = PlanFixtureBuilder(engine)
    builder.truncate_all()
    try:
        yield builder
    finally:
        builder.truncate_all()


class PlanFixtureBuilder:
    """Minimal helper — plans + plan-lifecycle events only."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def truncate_all(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                sa.text(
                    "TRUNCATE TABLE "
                    + ", ".join(_TEST_TABLES)
                    + " RESTART IDENTITY CASCADE"
                )
            )

    def make_plan(self, repo: str = "test/plan-status") -> uuid.UUID:
        with self.engine.begin() as conn:
            row = conn.execute(
                sa.text("INSERT INTO plans (repo) VALUES (:repo) RETURNING id"),
                {"repo": repo},
            ).one()
        return row.id

    def add_plan_event(
        self,
        plan_id: uuid.UUID,
        action: str,
        payload: dict | None = None,
    ) -> None:
        """Insert a ``plan.<action>`` event. Each call advances ``created_at``
        by relying on ``DEFAULT now()`` — Postgres ``now()`` returns the
        statement timestamp, which is monotonic in modern PG; the tests
        below also pad with a tiny sleep where ordering is load-bearing."""
        import json as _json

        with self.engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO events (entity_type, action, plan_id, payload) "
                    "VALUES ('plan', :a, :p, CAST(:pay AS jsonb))"
                ),
                {"a": action, "p": plan_id, "pay": _json.dumps(payload or {})},
            )


def _status_for(engine: Engine, plan_id: uuid.UUID) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT derived_status FROM plan_status WHERE id = :id"),
            {"id": plan_id},
        ).one_or_none()
    if row is None:
        return None
    return row.derived_status


# ── Transition tests ──────────────────────────────────────────────────────────


def test_status_drafting_when_no_events(
    engine: Engine, fixtures: PlanFixtureBuilder,
) -> None:
    """A bare plan row with no lifecycle events resolves to ``drafting``."""
    plan_id = fixtures.make_plan()
    assert _status_for(engine, plan_id) == "drafting"


def test_status_drafting_after_plan_registered(
    engine: Engine, fixtures: PlanFixtureBuilder,
) -> None:
    """``plan.registered`` resolves to ``drafting`` (the registered state
    *is* the drafting state — registration is the entry-event)."""
    plan_id = fixtures.make_plan()
    fixtures.add_plan_event(plan_id, "registered")
    assert _status_for(engine, plan_id) == "drafting"


def test_status_planning_after_plan_planning_started(
    engine: Engine, fixtures: PlanFixtureBuilder,
) -> None:
    plan_id = fixtures.make_plan()
    fixtures.add_plan_event(plan_id, "registered")
    time.sleep(0.001)
    fixtures.add_plan_event(plan_id, "planning_started")
    assert _status_for(engine, plan_id) == "planning"


def test_status_active_after_plan_activated(
    engine: Engine, fixtures: PlanFixtureBuilder,
) -> None:
    plan_id = fixtures.make_plan()
    fixtures.add_plan_event(plan_id, "registered")
    time.sleep(0.001)
    fixtures.add_plan_event(plan_id, "activated")
    assert _status_for(engine, plan_id) == "active"


def test_status_completed_after_active_then_completed(
    engine: Engine, fixtures: PlanFixtureBuilder,
) -> None:
    plan_id = fixtures.make_plan()
    fixtures.add_plan_event(plan_id, "registered")
    time.sleep(0.001)
    fixtures.add_plan_event(plan_id, "activated")
    time.sleep(0.001)
    fixtures.add_plan_event(plan_id, "completed")
    assert _status_for(engine, plan_id) == "completed"


def test_status_abandoned_after_active_then_abandoned(
    engine: Engine, fixtures: PlanFixtureBuilder,
) -> None:
    """The abandoned-anytime transition: ``plan.abandoned`` after
    ``plan.activated`` resolves to ``abandoned`` regardless of the prior
    active phase. The priority order requirement
    ``abandoned > completed > active`` holds because abandoned is the
    most recent lifecycle event."""
    plan_id = fixtures.make_plan()
    fixtures.add_plan_event(plan_id, "registered")
    time.sleep(0.001)
    fixtures.add_plan_event(plan_id, "activated")
    time.sleep(0.001)
    fixtures.add_plan_event(plan_id, "abandoned", payload={"reason": "redirected"})
    assert _status_for(engine, plan_id) == "abandoned"


def test_status_resolves_correctly_on_same_txn_event_ties(
    engine: Engine, fixtures: PlanFixtureBuilder,
) -> None:
    """Lifecycle events emitted in the same transaction share an identical
    ``created_at`` (Postgres ``now()`` returns the txn start time). The
    VIEW must use the explicit priority order — ``abandoned > completed >
    active > planning > drafting`` — not ``ORDER BY created_at DESC``
    alone, otherwise tied timestamps resolve arbitrarily. Scenario-1
    ``POST /plans`` with ``doc_content`` emits ``PlanRegistered`` +
    ``PlanActivated`` in the same txn; this test guards against the
    regression where that plan resolved to ``drafting``."""
    import sqlalchemy as sa

    plan_id = fixtures.make_plan()
    # Insert two lifecycle events with literally identical ``created_at``.
    # The VIEW must still resolve to ``active`` because ``activated`` has
    # higher priority than ``registered``.
    shared_ts = "2026-05-11T18:00:00+00:00"
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO events (entity_type, action, plan_id, "
                "payload, created_at) VALUES "
                "('plan', 'registered', :p, '{}'::jsonb, :ts), "
                "('plan', 'activated',  :p, '{}'::jsonb, :ts)"
            ),
            {"p": plan_id, "ts": shared_ts},
        )
    assert _status_for(engine, plan_id) == "active"


def test_status_explicit_priority_beats_recency(
    engine: Engine, fixtures: PlanFixtureBuilder,
) -> None:
    """Pathological case: ``plan.registered`` arrives *after*
    ``plan.activated``. (This shouldn't happen via legitimate code paths,
    but the VIEW must still resolve to ``active`` per the priority order,
    not ``drafting`` per recency.) Reproduces the underlying invariant
    the same-txn test asserts."""
    plan_id = fixtures.make_plan()
    fixtures.add_plan_event(plan_id, "activated")
    time.sleep(0.001)
    fixtures.add_plan_event(plan_id, "registered")  # later in wall time
    assert _status_for(engine, plan_id) == "active"


# ── Router smoke ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="module", autouse=True)
def wait_for_api(api_url: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{api_url}/health", timeout=2.0)
            if r.status_code == 200:
                return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"API not reachable at {api_url}")


def test_get_plan_response_includes_derived_status(
    engine: Engine, fixtures: PlanFixtureBuilder, api_url: str,
) -> None:
    """``GET /api/v1/plans/{id}`` exposes the derived_status field
    populated from the plan_status VIEW (per the 2026-05-11 closure
    plan D.4)."""
    plan_id = fixtures.make_plan()
    fixtures.add_plan_event(plan_id, "registered")
    time.sleep(0.001)
    fixtures.add_plan_event(plan_id, "activated")

    response = httpx.get(f"{api_url}/api/v1/plans/{plan_id}", timeout=5.0)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(plan_id)
    assert body["derived_status"] == "active"
