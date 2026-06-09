"""Capture a baseline DB-state sidecar for the trace-replay equivalence test.

The trace-replay test in
``services/api/tests/test_consumer_trace_replay.py`` asserts that the
post-extraction ``CoordinationConsumer + EventProjector + PlanRouter``
pipeline produces an identical observable side effect to the
pre-extraction monolith, when replayed against the same 1453-event
captured fixture.

The "identical" side is read from a baseline sidecar — a frozen JSON
file containing the post-replay row contents for each table the
consumer writes. This script generates that sidecar. Re-run it
whenever the pre-extraction code legitimately changes (e.g. an
upstream event-shape evolution forces a baseline refresh).

Usage:

    # Check out the baseline commit (typically main HEAD pre-PR).
    git checkout main

    # Bring up a clean local Postgres.
    treadmill-local up

    # Run the capture against the scrubbed fixture.
    TREADMILL_TEST_DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:15432/treadmill" \\
        uv run python scripts/capture_trace_baseline.py \\
            services/api/tests/fixtures/coordination_trace_b0cd81fc_events.jsonl.gz \\
            services/api/tests/fixtures/coordination_trace_b0cd81fc_baseline.json

    # Commit the new baseline alongside any code change that justified it.
    git add services/api/tests/fixtures/coordination_trace_b0cd81fc_baseline.json
    git commit -m "test(trace-replay): refresh baseline against <hash>"

The baseline file shape:

    {
      "schema": 1,
      "captured_against_commit": "<git sha>",
      "event_count": 1453,
      "tables": {
        "events": [ { ... row ... }, ... ],
        "workflow_run_steps": [ ... ],
        "task_prs": [ ... ]
      },
      "dispatcher_calls": [
        { "workflow_id": "...", "task_id": "...", "source_event_id": "..." },
        ...
      ],
      "reevaluate_call_count": <int>
    }

Schema versioning: bumping ``schema`` in the JSON header forces the
trace-replay test to refuse a stale baseline rather than silently
read it under an older invariant.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_SCHEMA_VERSION = 1


def _git_head_sha() -> str:
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True,
        ).strip()
        return sha
    except subprocess.SubprocessError:
        return "<not in a git checkout>"


def _serialise(value: Any) -> Any:
    """Make a row value JSON-friendly. UUIDs → str; datetimes → ISO."""
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


def _row_to_dict(row: sa.engine.Row) -> dict[str, Any]:
    return {k: _serialise(v) for k, v in row._mapping.items()}


class _RecordingDispatcher:
    """Stub dispatcher that records every ``publish`` call in order.

    The trace-replay assertion uses this to verify the post-extraction
    routing produces the same dispatched-run sequence as the baseline.
    """

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


async def _capture(fixture_path: Path, output_path: Path, database_url: str) -> None:
    """Replay the fixture, snapshot the resulting DB state, and write
    the baseline sidecar."""
    from treadmill_api.coordination.consumer import CoordinationConsumer

    async_url = database_url.replace("+psycopg", "+asyncpg")
    engine = create_async_engine(async_url, pool_pre_ping=True)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    dispatcher = _RecordingDispatcher()
    reevaluate_call_count = 0
    consumer = CoordinationConsumer(
        sqs_client=None,
        queue_url="<unused — direct handle() calls>",
        sessionmaker=sessionmaker,
        dispatcher=dispatcher,
    )

    # Intercept _reevaluate so we can count it without coupling the
    # test to dispatcher internals.
    original_reevaluate = consumer._reevaluate

    async def _wrapped_reevaluate(*args: Any, **kwargs: Any) -> Any:
        nonlocal reevaluate_call_count
        reevaluate_call_count += 1
        return await original_reevaluate(*args, **kwargs)

    consumer._reevaluate = _wrapped_reevaluate  # type: ignore[assignment]

    # Replay every event sequentially.
    event_count = 0
    with gzip.open(fixture_path, "rt") as f:
        for line in f:
            record = json.loads(line)
            await consumer.handle(record)
            event_count += 1

    # Snapshot the tables the consumer writes to.
    tables = ("events", "workflow_run_steps", "task_prs")
    table_rows: dict[str, list[dict[str, Any]]] = {}
    sync_engine = sa.create_engine(database_url, pool_pre_ping=True)
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

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w") as f:
        json.dump(baseline, f, indent=2, sort_keys=True)
    print(
        f"captured baseline: {event_count} events, "
        f"{len(dispatcher.calls)} dispatcher calls, "
        f"{reevaluate_call_count} _reevaluate calls -> {output_path}"
    )

    await engine.dispose()


def main() -> int:
    if len(sys.argv) != 3:
        sys.stderr.write(__doc__ or "")
        return 2

    import os

    fixture_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    database_url = os.environ.get(
        "TREADMILL_TEST_DATABASE_URL",
        "postgresql+psycopg://postgres:postgres@localhost:15432/treadmill",
    )

    asyncio.run(_capture(fixture_path, output_path, database_url))
    return 0


if __name__ == "__main__":
    sys.exit(main())
