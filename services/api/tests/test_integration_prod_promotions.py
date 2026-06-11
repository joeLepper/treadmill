"""Integration tests for the prod_promotions CAS guard — ADR-0088.

Pins the REAL SQL semantics the unit-test stub mirrors: the guarded
``UPDATE ... WHERE status = :expected AND expires_at > now()`` is the
load-bearing safety property (single-use + expiry, contract invariants
2 and 3) and must be exercised against live Postgres, not a stub.

Requires ``TREADMILL_INTEGRATION=1`` + ``treadmill-local up`` (same
convention as the other integration suites).
"""

from __future__ import annotations

import os
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
pytestmark = pytest.mark.skipif(
    not INTEGRATION,
    reason="set TREADMILL_INTEGRATION=1 to run; requires `treadmill-local up`",
)

DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://postgres:postgres@localhost:15432/treadmill"
)


@pytest.fixture(scope="module")
def engine():
    url = os.environ.get("TREADMILL_TEST_DATABASE_URL", DEFAULT_DATABASE_URL)
    eng = sa.create_engine(url, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module", autouse=True)
def migrations_applied(engine):
    api_dir = Path(__file__).resolve().parents[1]
    url = os.environ.get("TREADMILL_TEST_DATABASE_URL", DEFAULT_DATABASE_URL)
    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run(
        ["python3", "-m", "alembic", "upgrade", "head"],
        cwd=api_dir,
        env=env,
        check=True,
        capture_output=True,
    )


def _insert(engine, *, status="proposed", expires_delta_hours=48.0) -> uuid.UUID:
    pid = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO prod_promotions"
                " (proposal_id, repo, status, bundle, expires_at)"
                " VALUES (:pid, 'acme/widget', :status, '{}'::jsonb, :exp)"
            ),
            {
                "pid": pid,
                "status": status,
                "exp": datetime.now(timezone.utc)
                + timedelta(hours=expires_delta_hours),
            },
        )
    return pid


APPROVE_SQL = sa.text(
    "UPDATE prod_promotions SET status = 'approved', decided_by = :by,"
    " decided_at = now()"
    " WHERE proposal_id = :pid AND status = 'proposed'"
    " AND expires_at > now() RETURNING proposal_id"
)


def _cleanup(engine, pid: uuid.UUID) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text("DELETE FROM prod_promotions WHERE proposal_id = :pid"),
            {"pid": pid},
        )


def test_cas_approve_is_single_use(engine):
    pid = _insert(engine)
    try:
        with engine.begin() as conn:
            first = conn.execute(APPROVE_SQL, {"pid": pid, "by": "op-a"}).first()
        with engine.begin() as conn:
            second = conn.execute(APPROVE_SQL, {"pid": pid, "by": "op-b"}).first()
        assert first is not None
        assert second is None
        with engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT status, decided_by FROM prod_promotions"
                    " WHERE proposal_id = :pid"
                ),
                {"pid": pid},
            ).one()
        # The first writer won; the second CAS changed nothing.
        assert row.status == "approved"
        assert row.decided_by == "op-a"
    finally:
        _cleanup(engine, pid)


def test_cas_rejects_expired_but_undecided(engine):
    """The #303 review fold: status-only CAS would approve a stale
    proposal; the expiry predicate must block it in SQL."""
    pid = _insert(engine, expires_delta_hours=-1.0)
    try:
        with engine.begin() as conn:
            hit = conn.execute(APPROVE_SQL, {"pid": pid, "by": "op"}).first()
        assert hit is None
        with engine.connect() as conn:
            status = conn.execute(
                sa.text(
                    "SELECT status FROM prod_promotions WHERE proposal_id = :pid"
                ),
                {"pid": pid},
            ).scalar_one()
        assert status == "proposed"  # untouched; lazy expiry flips it on read
    finally:
        _cleanup(engine, pid)


def test_cas_rejects_wrong_prior_state(engine):
    pid = _insert(engine, status="rejected")
    try:
        with engine.begin() as conn:
            hit = conn.execute(APPROVE_SQL, {"pid": pid, "by": "op"}).first()
        assert hit is None
    finally:
        _cleanup(engine, pid)
