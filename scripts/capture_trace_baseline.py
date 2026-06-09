"""Capture a baseline DB-state sidecar for the trace-replay equivalence test.

Replays the synthetic trace fixture through ``CoordinationConsumer.handle()``
against a freshly-seeded database, snapshots the resulting state, and
writes the baseline sidecar that ``tests/test_consumer_trace_replay.py``
asserts against.

The fixture + seed manifest are produced by
``scripts/generate_synthetic_trace.py`` — regenerate them whenever the
event schema evolves so the harness keeps covering every routing path.

Usage::

    # Bring up a clean local Postgres (separate from the live dev-local
    # DB so the capture doesn't clobber state other sessions are
    # working against).
    docker run -d --rm --name treadmill-baseline-capture \\
        -p 15433:5432 -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=treadmill \\
        postgres:16
    cd services/api
    DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:15433/treadmill" \\
        uv run alembic upgrade head

    TREADMILL_TEST_DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:15433/treadmill" \\
        uv run python ../../scripts/capture_trace_baseline.py

    docker rm -f treadmill-baseline-capture

The script reads the fixture + seed from
``services/api/tests/fixtures/`` and writes the baseline sidecar to the
same directory.

Schema versioning: bumping ``_SCHEMA_VERSION`` in the JSON header forces
the trace-replay test to refuse a stale baseline rather than silently
read it under an older invariant.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_SCHEMA_VERSION = 2  # bumped from 1 — synthetic fixture replaces RAMJAC capture

_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "services" / "api" / "tests" / "fixtures"
_EVENTS_PATH = _FIXTURES_DIR / "coordination_trace_synthetic_events.jsonl.gz"
_SEED_PATH = _FIXTURES_DIR / "coordination_trace_synthetic_seed.json"
_BASELINE_PATH = _FIXTURES_DIR / "coordination_trace_synthetic_baseline.json"


def _git_head_sha() -> str:
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True,
        ).strip()
        return sha
    except subprocess.SubprocessError:
        return "<not in a git checkout>"


def _serialise(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_serialise(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialise(v) for k, v in value.items()}
    return repr(value)


# Wall-clock columns excluded from snapshots — server ``now()`` defaults
# differ between baseline capture and test-time replay. The harness asserts
# behavior equivalence, not wall-clock equivalence.
_VOLATILE_COLUMNS = frozenset({"created_at", "updated_at"})


def _row_to_dict(row: sa.engine.Row) -> dict[str, Any]:
    return {
        k: _serialise(v)
        for k, v in row._mapping.items()
        if k not in _VOLATILE_COLUMNS
    }


class _RecordingDispatcher:
    """Stub dispatcher — records every ``publish`` call in order so the
    equivalence assertion can compare routing-side effects directly."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def publish(
        self,
        *,
        workflow_id: str,
        task_id: uuid.UUID | str,
        source_event_id: uuid.UUID | str | None = None,
        **extra: Any,
    ) -> None:
        self.calls.append({
            "workflow_id": workflow_id,
            "task_id": str(task_id),
            "source_event_id": str(source_event_id) if source_event_id else None,
            "extra_keys": sorted(extra.keys()),
        })


# Tables seeded BEFORE replay. Order matters — each row's FKs reference
# rows in tables earlier in this list.
_SEED_TABLES = (
    "workflows",
    "workflow_versions",
    "roles",
    "workflow_version_steps",
    "plans",
    "tasks",
    "workflow_runs",
    "workflow_run_steps",
)


def seed_database_sync(
    engine: sa.engine.Engine,
    manifest: dict[str, list[dict[str, Any]]],
) -> None:
    """Insert the seed manifest rows in FK-respecting order.

    Uses a sync engine + parameterised INSERTs against the public
    information_schema columns — table shapes evolve and a too-specific
    INSERT statement here would be a constant maintenance tax. Instead
    we let SQLAlchemy reflect the table column names and pass only the
    keys present on each row.
    """
    meta = sa.MetaData()
    with engine.begin() as conn:
        for table_name in _SEED_TABLES:
            rows = manifest.get(table_name, [])
            if not rows:
                continue
            table = sa.Table(table_name, meta, autoload_with=conn)
            conn.execute(table.insert(), rows)


async def _capture(database_url: str) -> None:
    from treadmill_api.coordination.consumer import CoordinationConsumer

    async_url = database_url.replace("+psycopg", "+asyncpg")
    engine = create_async_engine(async_url, pool_pre_ping=True)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    # Seed the DB. We use a sync engine for SQLAlchemy reflection (the
    # async path requires `engine.run_sync(meta.reflect)` which is more
    # ceremony for the same result).
    sync_engine = sa.create_engine(database_url, pool_pre_ping=True)
    with _SEED_PATH.open() as f:
        manifest = json.load(f)
    seed_database_sync(sync_engine, manifest)
    print(
        f"seeded: "
        f"{len(manifest.get('plans', []))} plans, "
        f"{len(manifest.get('tasks', []))} tasks, "
        f"{len(manifest.get('workflow_runs', []))} runs, "
        f"{len(manifest.get('workflow_run_steps', []))} step rows."
    )

    dispatcher = _RecordingDispatcher()
    reevaluate_call_count = 0
    consumer = CoordinationConsumer(
        sqs_client=None,
        queue_url="<unused — direct handle() calls>",
        sessionmaker=sessionmaker,
        dispatcher=dispatcher,
    )

    original_reevaluate = consumer._reevaluate

    async def _wrapped_reevaluate(*args: Any, **kwargs: Any) -> Any:
        nonlocal reevaluate_call_count
        reevaluate_call_count += 1
        return await original_reevaluate(*args, **kwargs)

    consumer._reevaluate = _wrapped_reevaluate  # type: ignore[assignment]

    # Replay every event sequentially. The synthetic fixture is clean
    # by construction (the generator validates round-trip parsing
    # before exit) — a JSONDecodeError here is a real degradation, not
    # an expected skip path. Fail loudly.
    event_count = 0
    with gzip.open(_EVENTS_PATH, "rt") as f:
        for line_no, line in enumerate(f, start=1):
            record = json.loads(line)
            await consumer.handle(record)
            event_count += 1

    # Snapshot the tables the consumer writes to.
    tables = ("events", "workflow_run_steps", "task_prs")
    table_rows: dict[str, list[dict[str, Any]]] = {}
    with sync_engine.begin() as conn:
        for table in tables:
            rows = conn.execute(
                sa.text(f"SELECT * FROM {table} ORDER BY 1"),
            ).all()
            table_rows[table] = [_row_to_dict(r) for r in rows]
    sync_engine.dispose()

    baseline = {
        "schema": _SCHEMA_VERSION,
        "captured_against_commit": _git_head_sha(),
        "event_count": event_count,
        "tables": table_rows,
        "dispatcher_calls": dispatcher.calls,
        "reevaluate_call_count": reevaluate_call_count,
    }

    with _BASELINE_PATH.open("w") as f:
        json.dump(baseline, f, indent=2, sort_keys=True)
    print(
        f"captured baseline: {event_count} events, "
        f"{len(dispatcher.calls)} dispatcher calls, "
        f"{reevaluate_call_count} _reevaluate calls -> {_BASELINE_PATH}"
    )
    print(
        f"snapshot row counts — "
        f"events: {len(table_rows['events'])}, "
        f"workflow_run_steps: {len(table_rows['workflow_run_steps'])}, "
        f"task_prs: {len(table_rows['task_prs'])}"
    )

    await engine.dispose()


def main() -> int:
    database_url = os.environ.get(
        "TREADMILL_TEST_DATABASE_URL",
        "postgresql+psycopg://postgres:postgres@localhost:15433/treadmill",
    )
    asyncio.run(_capture(database_url))
    return 0


if __name__ == "__main__":
    sys.exit(main())
