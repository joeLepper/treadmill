"""Trace-replay equivalence gate for the Task 2A Phase 2 PlanRouter extraction.

The PlanRouter extraction in this PR moves 21 ``_maybe_*`` helpers + 5
entity-type handlers + ``_cross_step_dispatch`` + ``_reevaluate`` + the
D.8 webhook drain out of ``CoordinationConsumer`` and into the new
``PlanRouter``. The two collaborate via direct method call: the
consumer commits the projection transaction, then hands the same
record + typed payload to the router, which opens its own session and
runs routing decisions against the just-committed state.

Existing unit + integration tests cover *individual* routing helpers
in isolation. This test pins the COMPOSITION: replay a real 1453-event
captured trace through the post-extraction pipeline and assert
identical observable side effects against a frozen baseline sidecar
captured from pre-extraction code (committed alongside this test).

Skipped by default. To run:

    treadmill-local up
    TREADMILL_INTEGRATION=1 \\
        uv run pytest tests/test_consumer_trace_replay.py

The baseline sidecar lives at
``tests/fixtures/coordination_trace_b0cd81fc_baseline.json``. When the
pre-extraction code legitimately changes (e.g. an upstream event-shape
evolution forces a baseline refresh), regenerate it via
``scripts/capture_trace_baseline.py``; the script docstring documents
the procedure.
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from treadmill_api.coordination.consumer import CoordinationConsumer


INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
pytestmark = pytest.mark.skipif(
    not INTEGRATION,
    reason="set TREADMILL_INTEGRATION=1 to run; requires `treadmill-local up`",
)


DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://postgres:postgres@localhost:15432/treadmill"
)

# Schema version pinned in the sidecar. Bumped when the baseline shape
# evolves so a stale sidecar refuses rather than silently mis-matches.
_EXPECTED_BASELINE_SCHEMA = 1

_FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "coordination_trace_b0cd81fc_events.jsonl.gz"
)
_BASELINE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "coordination_trace_b0cd81fc_baseline.json"
)


# ── Fixtures (shared shape with test_integration_coordination_consumer.py) ─────


@pytest.fixture(scope="module")
def database_url() -> str:
    return os.environ.get("TREADMILL_TEST_DATABASE_URL", DEFAULT_DATABASE_URL)


@pytest.fixture(scope="module")
def async_database_url(database_url: str) -> str:
    return database_url.replace("+psycopg", "+asyncpg")


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
        cwd=services_api_dir,
        env=env,
        check=True,
    )


_TEST_TABLES = (
    "events",
    "workflow_run_steps",
    "workflow_runs",
    "task_prs",
    "task_dependencies",
    "task_board",
    "tasks",
    "plans",
    "workflow_version_steps",
    "workflow_versions",
    "workflows",
    "role_skills",
    "role_hooks",
    "skills",
    "hooks",
    "roles",
    "event_triggers",
)


@pytest.fixture
def truncate(engine: Engine) -> Iterator[None]:
    def _do() -> None:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "TRUNCATE TABLE "
                    + ", ".join(_TEST_TABLES)
                    + " RESTART IDENTITY CASCADE"
                )
            )
    _do()
    yield
    _do()


@pytest_asyncio.fixture
async def async_session_maker(
    async_database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(async_database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


# ── Helpers ────────────────────────────────────────────────────────────────────


class _RecordingDispatcher:
    """Mirror of the same stub in ``scripts/capture_trace_baseline.py``.

    Records every dispatcher.publish call so the trace-replay assertion
    can compare the routing sequence directly.
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


def _row_to_dict(row: sa.engine.Row) -> dict[str, Any]:
    return {k: _serialise(v) for k, v in row._mapping.items()}


def _load_baseline() -> dict[str, Any]:
    if not _BASELINE_PATH.exists():
        pytest.fail(
            f"baseline sidecar missing: {_BASELINE_PATH}. "
            "Regenerate via scripts/capture_trace_baseline.py (see "
            "the script's docstring for the procedure)."
        )
    with _BASELINE_PATH.open() as f:
        baseline = json.load(f)
    if baseline.get("schema") != _EXPECTED_BASELINE_SCHEMA:
        pytest.fail(
            f"baseline schema mismatch: file says "
            f"{baseline.get('schema')!r}, test expects "
            f"{_EXPECTED_BASELINE_SCHEMA!r}. Regenerate the sidecar after "
            "bumping the schema."
        )
    return baseline


# ── The replay test ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trace_replay_matches_baseline(
    engine: Engine,
    async_session_maker: async_sessionmaker[AsyncSession],
    truncate: None,
) -> None:
    """Replay the captured trace through the post-extraction pipeline and
    assert identical observable side effects vs the baseline sidecar.

    Equality is checked on:

    1. ``events`` table — every audit row, by id.
    2. ``workflow_run_steps`` table — every step row, by id.
    3. ``task_prs`` table — every PR-bridge row, by (task_id, repo, pr_number).
    4. Dispatcher publish sequence — workflow_id × task_id × source_event_id,
       in order, captured via the recording stub.
    5. ``_reevaluate`` call count — total invocations across the replay.

    Any deviation is a routing-behavior regression introduced by the
    Phase 2 extraction.
    """
    if not _FIXTURE_PATH.exists():
        pytest.fail(f"trace fixture missing: {_FIXTURE_PATH}")

    baseline = _load_baseline()

    dispatcher = _RecordingDispatcher()
    reevaluate_call_count = 0
    consumer = CoordinationConsumer(
        sqs_client=None,
        queue_url="<unused — direct handle() calls>",
        sessionmaker=async_session_maker,
        dispatcher=dispatcher,
    )

    original_reevaluate = consumer._reevaluate

    async def _wrapped_reevaluate(*args: Any, **kwargs: Any) -> Any:
        nonlocal reevaluate_call_count
        reevaluate_call_count += 1
        return await original_reevaluate(*args, **kwargs)

    consumer._reevaluate = _wrapped_reevaluate  # type: ignore[assignment]

    event_count = 0
    with gzip.open(_FIXTURE_PATH, "rt") as f:
        for line in f:
            record = json.loads(line)
            await consumer.handle(record)
            event_count += 1

    assert event_count == baseline["event_count"], (
        f"replay processed {event_count} events; baseline captured "
        f"{baseline['event_count']}. The fixture changed under the test."
    )

    # 1-3. Compare table snapshots.
    snapshot: dict[str, list[dict[str, Any]]] = {}
    for table in ("events", "workflow_run_steps", "task_prs"):
        with engine.begin() as conn:
            rows = conn.execute(
                sa.text(f"SELECT * FROM {table} ORDER BY 1"),
            ).all()
            snapshot[table] = [_row_to_dict(r) for r in rows]

    for table in ("events", "workflow_run_steps", "task_prs"):
        actual = snapshot[table]
        expected = baseline["tables"][table]
        assert actual == expected, (
            f"{table}: post-extraction snapshot diverges from baseline. "
            f"Actual rows: {len(actual)}; baseline rows: {len(expected)}. "
            "Either the extraction changed routing behavior (find the "
            "diff via fixtures/coordination_trace_b0cd81fc_baseline.json) "
            "or the baseline is stale (regenerate via "
            "scripts/capture_trace_baseline.py)."
        )

    # 4. Dispatcher publish sequence.
    assert dispatcher.calls == baseline["dispatcher_calls"], (
        f"dispatcher publish sequence diverges: replay made "
        f"{len(dispatcher.calls)} calls; baseline recorded "
        f"{len(baseline['dispatcher_calls'])} calls. Order matters — "
        "this asserts the post-extraction routing fires workflows in "
        "the same order as the pre-extraction code."
    )

    # 5. _reevaluate call count.
    assert reevaluate_call_count == baseline["reevaluate_call_count"], (
        f"_reevaluate invocation count diverges: replay fired "
        f"{reevaluate_call_count}; baseline recorded "
        f"{baseline['reevaluate_call_count']}."
    )
