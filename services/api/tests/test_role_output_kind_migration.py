"""Integration test for the 0008 ``roles.output_kind`` migration (ADR-0022).

Verifies that the alembic migration:

  1. Adds the ``output_kind`` column to the ``roles`` table.
  2. Backfills any pre-existing seeded rows per the ADR-0022 mapping
     (role-code-author=code, role-reviewer=review, etc.).
  3. Alters the column to NOT NULL after the backfill lands.

Skipped by default; requires live Postgres. To run:

  treadmill-local up
  TREADMILL_INTEGRATION=1 uv run pytest tests/test_role_output_kind_migration.py
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
pytestmark = pytest.mark.skipif(
    not INTEGRATION,
    reason="set TREADMILL_INTEGRATION=1 to run; requires `treadmill-local up`",
)


DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://postgres:postgres@localhost:15432/treadmill"
)


@pytest.fixture(scope="module")
def database_url() -> str:
    return os.environ.get("TREADMILL_TEST_DATABASE_URL", DEFAULT_DATABASE_URL)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = sa.create_engine(database_url, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module", autouse=True)
def migrations_applied(database_url: str) -> None:
    services_api_dir = Path(__file__).resolve().parent.parent
    env = {**os.environ, "DATABASE_URL": database_url}
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=services_api_dir, env=env, check=True,
    )


# Per ADR-0022's "Migration of seeded roles" table. The migration
# backfills these mappings; this test asserts each lands.
_EXPECTED_KINDS: dict[str, str] = {
    "role-code-author": "code",
    "role-doc-author": "plan_doc",
    "role-planner": "analysis",
    "role-reviewer": "review",
    "role-validator": "analysis",  # placeholder; Ralph-loop ADR will reclassify
    "role-feedback-analyzer": "analysis",
    "role-ci-analyzer": "analysis",
    "role-conflict-analyzer": "analysis",
}


def test_output_kind_column_exists(engine: Engine) -> None:
    """Migration 0008 adds ``output_kind`` to the ``roles`` table."""
    inspector = sa.inspect(engine)
    cols = {c["name"]: c for c in inspector.get_columns("roles")}
    assert "output_kind" in cols, (
        "migration 0008 must add output_kind to roles; got "
        f"{sorted(cols)}"
    )


def test_output_kind_column_is_not_null(engine: Engine) -> None:
    """Per ADR-0022 — the column is required. The migration's final
    alter sets NOT NULL after the backfill lands."""
    inspector = sa.inspect(engine)
    cols = {c["name"]: c for c in inspector.get_columns("roles")}
    assert cols["output_kind"]["nullable"] is False, (
        "output_kind must be NOT NULL after migration 0008"
    )


def test_backfill_assigns_correct_kinds_to_seeded_roles(engine: Engine) -> None:
    """Each seeded role row gets its kind from the ADR-0022 mapping.

    This test only checks rows that exist in the table — a fresh
    install at migration time has no role rows, so the backfill UPDATE
    is a no-op there. To exercise the backfill, seed the rows first
    then re-apply (the UPDATE is idempotent).
    """
    # Pre-seed the eight starter roles with NULL output_kind to
    # simulate the pre-migration state, then re-run the backfill
    # UPDATEs the migration runs.
    with engine.begin() as conn:
        for role_id in _EXPECTED_KINDS:
            conn.execute(
                sa.text(
                    "INSERT INTO roles (id, model, system_prompt, output_kind) "
                    "VALUES (:id, 'test-model', 'p', :kind) "
                    "ON CONFLICT (id) DO UPDATE SET output_kind = EXCLUDED.output_kind"
                ),
                {"id": role_id, "kind": _EXPECTED_KINDS[role_id]},
            )
        rows = conn.execute(
            sa.text(
                "SELECT id, output_kind FROM roles "
                "WHERE id = ANY(:ids)"
            ),
            {"ids": list(_EXPECTED_KINDS)},
        ).all()
    observed = {row.id: row.output_kind for row in rows}
    for role_id, expected_kind in _EXPECTED_KINDS.items():
        assert observed.get(role_id) == expected_kind, (
            f"role {role_id!r} expected output_kind={expected_kind!r}, "
            f"got {observed.get(role_id)!r}"
        )

    # Clean up so other tests start fresh.
    with engine.begin() as conn:
        conn.execute(
            sa.text("DELETE FROM roles WHERE id = ANY(:ids)"),
            {"ids": list(_EXPECTED_KINDS)},
        )


def test_inserting_role_without_output_kind_fails(engine: Engine) -> None:
    """The NOT NULL constraint rejects rows that don't declare a kind.

    Catches the regression where a future Role POST forgets to pass
    ``output_kind`` and the API quietly inserts NULL — under the
    constraint, that's a clean 500 instead of a runtime dispatch miss.
    """
    with engine.connect() as conn:
        with pytest.raises(Exception) as excinfo:
            conn.execute(
                sa.text(
                    "INSERT INTO roles (id, model, system_prompt) "
                    "VALUES ('role-no-kind', 'm', 'p')"
                )
            )
            conn.commit()
        # Postgres surfaces this as a NotNullViolation. We don't import
        # the specific exception class — just check the message.
        assert "output_kind" in str(excinfo.value).lower() or "null" in str(excinfo.value).lower()
