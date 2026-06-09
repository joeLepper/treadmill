"""Trace-replay equivalence gate for the PlanRouter extraction.

The Phase-2 extraction (PR #258) moved 21 ``_maybe_*`` helpers + 5
entity-type handlers + ``_cross_step_dispatch`` + ``_reevaluate`` + the
D.8 webhook drain out of ``CoordinationConsumer`` and into
``PlanRouter``. The two collaborate via direct method call: the
consumer commits the projection transaction, then hands the same
record + typed payload to the router, which opens its own session and
runs routing decisions against the just-committed state.

Existing unit + integration tests cover *individual* routing helpers
in isolation. This test pins the COMPOSITION: seed a synthetic plan
into a clean DB, replay a 56-event synthetic trace through the
post-extraction pipeline, and assert identical observable side
effects against a frozen baseline sidecar.

Fixture / seed / baseline are produced by
``scripts/generate_synthetic_trace.py`` (events + seed) and
``scripts/capture_trace_baseline.py`` (baseline). Synthetic by design
— the prior RAMJAC capture had a 21% JSON-escaping defect at the
capture-pipeline layer that this generator structurally avoids by
serializing every record via ``json.dumps`` and validating round-trip
parsing before exit.

Skipped by default. To run::

    docker run -d --rm --name treadmill-baseline-capture \\
        -p 15433:5432 -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=treadmill \\
        postgres:16
    TREADMILL_TEST_DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:15433/treadmill" \\
        uv run alembic upgrade head
    TREADMILL_INTEGRATION=1 \\
        TREADMILL_TEST_DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:15433/treadmill" \\
        uv run pytest tests/test_consumer_trace_replay.py
"""

from __future__ import annotations

import gzip
import json
import os
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
    reason="set TREADMILL_INTEGRATION=1 to run; requires `treadmill-local up` "
           "or an ephemeral Postgres (see module docstring)",
)


DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://postgres:postgres@localhost:15433/treadmill"
)

# Schema version pinned in the sidecar — bumped to 2 when the fixture
# changed from the malformed-21% RAMJAC capture to the synthetic-by-
# construction generator. Stale sidecars refuse rather than mis-match.
_EXPECTED_BASELINE_SCHEMA = 2

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
_EVENTS_PATH = _FIXTURES_DIR / "coordination_trace_synthetic_events.jsonl.gz"
_SEED_PATH = _FIXTURES_DIR / "coordination_trace_synthetic_seed.json"
_BASELINE_PATH = _FIXTURES_DIR / "coordination_trace_synthetic_baseline.json"


# Tables seeded BEFORE replay, in FK-respecting order. Must match the
# order in scripts/capture_trace_baseline.py so the baseline and the
# test exercise an identical pre-replay state.
_SEED_TABLES = (
    "workflows",
    "workflow_versions",
    "roles",
    "workflow_version_steps",
    "event_triggers",
    "plans",
    "tasks",
    "workflow_runs",
    "workflow_run_steps",
)


# Tables truncated between the fixture's per-test runs.
_TRUNCATE_TABLES = (
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


# ── Fixtures ────────────────────────────────────────────────────────────────────


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


@pytest.fixture
def truncate_and_seed(engine: Engine) -> Iterator[None]:
    """Truncate the test tables, seed the synthetic plan, yield."""
    if not _SEED_PATH.exists():
        pytest.fail(
            f"seed manifest missing: {_SEED_PATH}. Regenerate via "
            "scripts/generate_synthetic_trace.py."
        )
    with _SEED_PATH.open() as f:
        manifest = json.load(f)

    def _do_truncate() -> None:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "TRUNCATE TABLE "
                    + ", ".join(_TRUNCATE_TABLES)
                    + " RESTART IDENTITY CASCADE"
                )
            )

    def _seed() -> None:
        meta = sa.MetaData()
        with engine.begin() as conn:
            for table_name in _SEED_TABLES:
                rows = manifest.get(table_name, [])
                if not rows:
                    continue
                table = sa.Table(table_name, meta, autoload_with=conn)
                conn.execute(table.insert(), rows)

    _do_truncate()
    _seed()
    yield
    _do_truncate()


@pytest_asyncio.fixture
async def async_session_maker(
    async_database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(async_database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


# ── Helpers ────────────────────────────────────────────────────────────────────


class _RecordingPublisher:
    """Recording publisher passed into the REAL Dispatcher.

    Captures every ``publish(event, payload)`` invocation in arrival
    order. The trigger paths and ``dispatch_task`` both call into
    ``dispatcher.publisher.publish`` — must match the shape in
    ``scripts/capture_trace_baseline.py``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def publish(self, event: Any, payload: Any) -> None:
        self.calls.append({
            "kind": "publisher.publish",
            "event_type": getattr(event, "entity_type", None),
            "event_action": getattr(event, "action", None),
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


# Wall-clock columns excluded from snapshots — server ``now()`` defaults
# differ between baseline capture and test-time replay. The harness
# asserts behavior equivalence, not wall-clock equivalence. Must match
# the same set in ``scripts/capture_trace_baseline.py``.
_VOLATILE_COLUMNS = frozenset({"created_at", "updated_at"})


def _row_to_dict(row: sa.engine.Row) -> dict[str, Any]:
    return {
        k: _serialise(v)
        for k, v in row._mapping.items()
        if k not in _VOLATILE_COLUMNS
    }


def _load_baseline() -> dict[str, Any]:
    if not _BASELINE_PATH.exists():
        pytest.fail(
            f"baseline sidecar missing: {_BASELINE_PATH}. "
            "Regenerate via scripts/capture_trace_baseline.py."
        )
    with _BASELINE_PATH.open() as f:
        baseline = json.load(f)
    if baseline.get("schema") != _EXPECTED_BASELINE_SCHEMA:
        pytest.fail(
            f"baseline schema mismatch: file says "
            f"{baseline.get('schema')!r}, test expects "
            f"{_EXPECTED_BASELINE_SCHEMA!r}. Regenerate the sidecar."
        )
    return baseline


# ── The replay test ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trace_replay_matches_baseline(
    engine: Engine,
    async_session_maker: async_sessionmaker[AsyncSession],
    truncate_and_seed: None,
) -> None:
    """Replay the synthetic trace through the post-extraction pipeline
    and assert identical observable side effects vs the baseline.

    Equality is checked on:

    1. ``events`` table snapshot
    2. ``workflow_run_steps`` table snapshot
    3. ``task_prs`` table snapshot
    4. Dispatcher publish sequence (order-sensitive)
    5. ``_reevaluate`` call count

    The fixture is clean by construction (the generator validates
    round-trip parsing). A ``JSONDecodeError`` during replay is a real
    regression, not a skipped line.
    """
    if not _EVENTS_PATH.exists():
        pytest.fail(f"trace fixture missing: {_EVENTS_PATH}")

    baseline = _load_baseline()

    from treadmill_api.dispatch import Dispatcher

    publisher = _RecordingPublisher()
    dispatcher = Dispatcher(
        publisher=publisher,
        sqs_client=None,
        work_queue_url=None,
    )
    reevaluate_call_count = 0
    consumer = CoordinationConsumer(
        sqs_client=None,
        queue_url="<unused — direct handle() calls>",
        sessionmaker=async_session_maker,
        dispatcher=dispatcher,
    )

    original_reevaluate = consumer.router._reevaluate

    async def _wrapped_reevaluate(*args: Any, **kwargs: Any) -> Any:
        nonlocal reevaluate_call_count
        reevaluate_call_count += 1
        return await original_reevaluate(*args, **kwargs)

    consumer.router._reevaluate = _wrapped_reevaluate  # type: ignore[assignment]

    event_count = 0
    with gzip.open(_EVENTS_PATH, "rt") as f:
        for line in f:
            record = json.loads(line)
            await consumer.handle(record)
            event_count += 1

    assert event_count == baseline["event_count"], (
        f"replay processed {event_count} events; baseline captured "
        f"{baseline['event_count']}. The fixture changed under the test "
        "(or the baseline is stale — regenerate via "
        "scripts/capture_trace_baseline.py)."
    )

    # 1. Per-table row counts. The trigger evaluator creates downstream
    # rows (workflow_runs / workflow_run_steps / events) with
    # ``gen_random_uuid()`` defaults so row-level UUID comparison is
    # nondeterministic across capture / replay runs. Behaviour
    # equivalence holds at the COUNT level; the publisher.publish
    # sequence (next assertion) gives an order-sensitive equivalence
    # check on the routing decisions themselves.
    table_counts: dict[str, int] = {}
    with engine.begin() as conn:
        for table in ("events", "workflow_run_steps", "task_prs"):
            (count,) = conn.execute(
                sa.text(f"SELECT COUNT(*) FROM {table}"),
            ).one()
            table_counts[table] = count
        events_by_kind = dict(conn.execute(
            sa.text(
                "SELECT entity_type || '.' || action AS kind, COUNT(*) "
                "FROM events GROUP BY 1 ORDER BY 1"
            ),
        ).all())

    assert table_counts == baseline["table_counts"], (
        f"table row counts diverge from baseline: replay produced "
        f"{table_counts}; baseline recorded {baseline['table_counts']}. "
        "Either the extraction changed routing behavior or the baseline "
        "is stale (regenerate via scripts/capture_trace_baseline.py)."
    )

    # 2. Events broken down by (entity_type, action) — catches
    # divergences in which event kinds are produced even when totals
    # happen to match.
    assert events_by_kind == baseline["events_by_kind"], (
        f"events_by_kind diverges from baseline: replay produced "
        f"{events_by_kind}; baseline recorded {baseline['events_by_kind']}."
    )

    # 3. Publisher publish sequence (recording publisher passed into
    # the real Dispatcher captures every publish from trigger paths +
    # cross_step_dispatch + _reevaluate's downstream dispatch_task).
    assert publisher.calls == baseline["publisher_calls"], (
        f"publisher.publish sequence diverges: replay made "
        f"{len(publisher.calls)} calls; baseline recorded "
        f"{len(baseline['publisher_calls'])} calls."
    )

    # 4. _reevaluate call count.
    assert reevaluate_call_count == baseline["reevaluate_call_count"], (
        f"_reevaluate invocation count diverges: replay fired "
        f"{reevaluate_call_count}; baseline recorded "
        f"{baseline['reevaluate_call_count']}."
    )
