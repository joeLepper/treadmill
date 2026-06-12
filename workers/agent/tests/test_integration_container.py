"""Worker container integration test against a live substrate (B.12).

The Week 2 close-out's container smoke ran by hand:
``treadmill-local up`` + ``treadmill-local repo init`` + ``curl`` + a
manual ``docker run`` of the agent image, with the operator squinting
at logs for success. Unrepeatable in CI. This test automates that loop
under pytest control so the regression surface is checkable.

Gating
------

  * ``TREADMILL_INTEGRATION=1`` — opt-in switch (mirrors the existing
    integration tests in ``services/api/tests/``). The test assumes the
    substrate is already up unless ``TREADMILL_LOCAL_HARNESS=1`` is
    *also* set, in which case the ``local_substrate`` fixture (Phase 3
    C.7) brings it up + tears it down.

  * Default: dry-run mode. The worker is launched with
    ``TREADMILL_AGENT_DRY_RUN=1`` overridden at the docker-run level —
    even though B.9 removed it from the CDK env. The override keeps
    the test free of LLM cost and stable across CI runners.

  * ``TREADMILL_REAL_CLAUDE=1`` — additional escalation. When set, the
    test omits the dry-run env override so the container runs the real
    Claude path. Same shape of assertions either way (B.7 removed
    the ``gh`` PR-opening path, so there's never a ``task_prs`` row).

Sequence
--------

  1. Substrate up (fixture or assumed).
  2. Build the ``treadmill-agent:dev`` image if it isn't present (the
     ``LocalRuntime`` provisioner refuses to pull local-only tags).
  3. Seed a local bare repo (the worker clones from ``file://``).
  4. POST to ``/api/v1/plans`` with a one-step ``wf-author`` workflow.
  5. Manually launch the agent container with the right env override;
     don't rely on the autoscaler so the test's wait window stays
     deterministic regardless of scaling intervals.
  6. Wait up to 60s for ``workflow_run_steps.status='completed'``.
  7. Assert the events table contains the lifecycle rows.

Cleanup
-------

The launched container is removed in a finally block; truncate of the
test rows is left to the existing per-test truncate convention (this
test owns its own bare repo so doesn't collide with other tests).
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine


INTEGRATION = os.environ.get("TREADMILL_INTEGRATION") == "1"
TEST_DB_URL = os.environ.get("TREADMILL_TEST_DATABASE_URL")
# Task 3aaba5e7: NO live-API default. TREADMILL_API_URL is ambient in
# every team-session env (it points at the LIVE deployment), so a
# fallback to it — or to localhost:8088, which IS the live stack on
# the operator host — silently sends this file's writes to
# production state. A DEDICATED test var makes that an explicit act,
# mirroring TREADMILL_TEST_DATABASE_URL.
TEST_API_URL = os.environ.get("TREADMILL_TEST_API_URL")
REAL_CLAUDE = os.environ.get("TREADMILL_REAL_CLAUDE") == "1"
USE_HARNESS = os.environ.get("TREADMILL_LOCAL_HARNESS") == "1"


pytestmark = pytest.mark.skipif(
    not (INTEGRATION and TEST_DB_URL and TEST_API_URL),
    reason=(
        "set TREADMILL_INTEGRATION=1 and TREADMILL_TEST_DATABASE_URL (a DEDICATED test database) and TREADMILL_TEST_API_URL (a test API instance, never the live one) to run the worker container "
        "integration test; requires `treadmill-local up` (or "
        "TREADMILL_LOCAL_HARNESS=1 to use the bring-up fixture)"
    ),
)


# Endpoints mirror those used by services/api/tests/test_integration_*.
DEFAULT_AWS_ENDPOINT = "http://localhost:5001"

AGENT_IMAGE = "treadmill-agent:dev"
AGENT_NETWORK = "treadmill-local"


# ── Pull in the bring-up fixture conditionally ───────────────────────────────


# Importing `local_substrate` even when the gate is off is fine — the
# fixture's body raises ``pytest.skip`` immediately unless
# TREADMILL_LOCAL_HARNESS=1 is set. We only *use* it from a test signature
# when the user opted in, otherwise tests rely on an already-up substrate.
try:
    from treadmill_local.pytest_harness import local_substrate  # noqa: F401
except ImportError:
    # ``treadmill-local`` may not be installed in some workspace configs
    # (the worker package only declares it as a dev requirement once the
    # integration tests are wired). Tests will skip with a clear message.
    local_substrate = None  # type: ignore[assignment]


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def api_url() -> str:
    return TEST_API_URL


@pytest.fixture(scope="module")
def database_url() -> str:
    return TEST_DB_URL


@pytest.fixture(scope="module")
def aws_endpoint_url() -> str:
    return os.environ.get("AWS_ENDPOINT_URL", DEFAULT_AWS_ENDPOINT)


@pytest.fixture(scope="module")
def engine(database_url: str) -> Iterator[Engine]:
    eng = sa.create_engine(database_url, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture(scope="module")
def boto_kwargs(aws_endpoint_url: str) -> dict[str, Any]:
    return dict(
        endpoint_url=aws_endpoint_url,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


@pytest.fixture(scope="module")
def client(api_url: str) -> Iterator[httpx.Client]:
    """Wait for the API health endpoint, then yield an httpx client.

    When the harness fixture is also used, by the time pytest evaluates
    this fixture the substrate is already up; this just protects against
    a slow start.
    """
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{api_url}/health", timeout=2.0)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.5)
    with httpx.Client(base_url=api_url, timeout=10.0) as c:
        yield c


@pytest.fixture(scope="module", autouse=True)
def _substrate_up(request: pytest.FixtureRequest) -> None:
    """Bring the substrate up via the C.7 fixture when the harness gate
    is set; otherwise no-op (the substrate is assumed to be running).

    Wiring the fixture this way means the test signature does NOT need
    to mention ``local_substrate`` — pulling it in at module scope keeps
    the test functions readable.
    """
    if USE_HARNESS and local_substrate is not None:
        request.getfixturevalue("local_substrate")
    # Otherwise: do nothing. The test's wait-for-API loop handles
    # transient unreachability with a clear timeout.


@pytest.fixture(scope="module", autouse=True)
def migrations_applied(database_url: str, _substrate_up: None) -> None:
    """Apply alembic migrations against the live Postgres.

    Mirrors the same fixture in
    ``services/api/tests/test_integration_eventbus_and_pending.py``. The
    ``local_substrate`` bring-up starts a fresh Postgres container with
    no schema, so this is required for any test that touches DB rows.
    """
    services_api_dir = (
        Path(__file__).resolve().parents[3] / "services" / "api"
    )
    if not (services_api_dir / "alembic.ini").is_file():
        pytest.skip(
            f"could not locate alembic.ini under {services_api_dir}; "
            "the integration test needs the API package on disk"
        )
    env = {**os.environ, "DATABASE_URL": database_url}
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=services_api_dir,
        env=env,
        check=True,
    )


@pytest.fixture(scope="module", autouse=True)
def agent_image_built() -> None:
    """Make sure ``treadmill-agent:dev`` exists locally; build it if not.

    ``LocalRuntime._ensure_image`` refuses to pull local-only tags so the
    image MUST be present before we ``docker run`` it. The build is
    idempotent — a re-run with a clean cache is fast.

    Build context is the workspace root (not ``workers/agent/``) because
    the agent's ``pyproject.toml`` declares ``treadmill-api`` as a
    workspace source — the API package must be present at build time
    so pip can resolve the editable install.
    """
    inspect = subprocess.run(
        ["docker", "image", "inspect", AGENT_IMAGE],
        capture_output=True,
    )
    if inspect.returncode == 0:
        return
    repo_root = Path(__file__).resolve().parents[3]
    dockerfile = repo_root / "workers" / "agent" / "Dockerfile"
    if not dockerfile.is_file():
        pytest.skip(
            f"could not locate {dockerfile} to build the agent image"
        )
    subprocess.run(
        [
            "docker", "build",
            "-t", AGENT_IMAGE,
            "-f", str(dockerfile),
            str(repo_root),
        ],
        check=True,
    )


@pytest.fixture
def bare_repo(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, Path]:
    """Seed a unique bare repo under the host-side path the runtime mounts
    into the worker (``.treadmill-local/repos``). The path is shared
    across the worker container and host because the runtime mounts that
    directory into ``/var/treadmill/repos`` (see ``LocalRuntime._volumes_for``).
    """
    from treadmill_local.repos import init_bare_repo

    repo = f"treadmill/agent-int-{uuid.uuid4().hex[:8]}"
    bare_root = Path.cwd() / ".treadmill-local" / "repos"
    bare = init_bare_repo(bare_root, repo)
    return repo, bare


_TEST_TABLES = (
    "plans", "tasks", "task_prs", "task_dependencies",
    "workflow_runs", "workflow_run_steps", "events",
    "event_triggers",
)


@pytest.fixture
def truncate(engine: Engine) -> Iterator[None]:
    """Wipe the rows this test owns. ``workflows`` / ``roles`` /
    ``skills`` / ``hooks`` are NOT truncated — they are seeded by the
    install (or another test in the session) and the integration test
    needs ``wf-author`` + ``role-author`` to already exist."""
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


# ── Helpers ───────────────────────────────────────────────────────────────────


def _ensure_starters_seeded(client: httpx.Client) -> None:
    """Idempotently seed the starter workflows + roles so ``wf-author``
    + ``role-author`` exist before we POST a plan referencing them.

    Uses the starters module's ``seed`` function via a stub client that
    forwards to the live API.
    """
    from treadmill_api.starters import seed
    from treadmill_cli.api_client import ApiError

    class _Forwarder:
        def __init__(self, c: httpx.Client) -> None:
            self._c = c

        def _request(self, method: str, path: str, **kwargs: Any) -> Any:
            response = self._c.request(method, path, **kwargs)
            if response.status_code >= 400:
                try:
                    detail = response.json().get("detail", response.text)
                except Exception:
                    detail = response.text
                raise ApiError(response.status_code, detail)
            if response.status_code == 204 or not response.content:
                return None
            return response.json()

    seed(_Forwarder(client))


def _sqs_work_queue_url(boto_kwargs: dict[str, Any]) -> str:
    """Look up the work queue URL via moto / SQS list-queues.

    The CDK gives the queue physical name ``treadmill-spike-work.fifo``
    (the ``-spike-`` infix comes from the stack name). Match by suffix
    + excluding DLQs so a future rename of the stack still works.
    """
    sqs = boto3.client("sqs", **boto_kwargs)
    resp = sqs.list_queues()
    urls = resp.get("QueueUrls", [])
    for url in urls:
        # Match the live work queue, not its DLQ.
        if (url.endswith("work.fifo") or url.endswith("work")) and "dlq" not in url:
            return url
    raise RuntimeError(f"could not find work queue in {urls!r}")


def _events_topic_arn(boto_kwargs: dict[str, Any]) -> str:
    sns = boto3.client("sns", **boto_kwargs)
    topics = sns.list_topics().get("Topics", [])
    for t in topics:
        if t["TopicArn"].endswith(":treadmill-events"):
            return t["TopicArn"]
    raise RuntimeError(f"could not find events topic in {topics!r}")


def _run_worker_container(
    *,
    work_queue_url: str,
    events_topic_arn: str,
    dry_run: bool,
    moto_container_name: str = "treadmill-local-moto",
) -> str:
    """Manually start the agent container with env overrides. Returns the
    container id (short form).

    We do NOT use ``LocalRuntime.start_worker_once`` because that path
    uses the spec env verbatim — and the dry-run override only exists
    at the test level (per closure decision #7/B.9: dry-run is gone
    from CDK; tests that want it set it at runtime).
    """
    # The work queue URL embeds moto's host-mapped port (``localhost:5001``).
    # From inside the worker container, ``localhost`` is the container itself,
    # not the host. Rewrite to the docker-network hostname so the SQS client
    # actually reaches moto.
    work_queue_url_internal = work_queue_url.replace(
        "localhost:5001", f"{moto_container_name}:5000",
    ).replace(
        "127.0.0.1:5001", f"{moto_container_name}:5000",
    )

    env: dict[str, str] = {
        "WORK_QUEUE_URL": work_queue_url_internal,
        "EVENTS_TOPIC_ARN": events_topic_arn,
        "AWS_ENDPOINT_URL": f"http://{moto_container_name}:5000",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "TREADMILL_API_URL": "http://treadmill-api:8088",
        "REPO_MODE": "local",
        "EXIT_AFTER_STEP": "true",
        # Aggressive long-poll so the worker doesn't hang past the test
        # window if something upstream went wrong.
        "POLL_WAIT_SECONDS": "5",
    }
    if dry_run:
        env["TREADMILL_AGENT_DRY_RUN"] = "1"

    name = f"treadmill-test-agent-{uuid.uuid4().hex[:8]}"
    # Note: no ``--rm`` so the container sticks around after it exits.
    # The test inspects its logs on failure for triage, then deletes
    # it in the finally block.
    cmd: list[str] = [
        "docker", "run", "-d",
        "--name", name,
        "--network", AGENT_NETWORK,
        "--label", "treadmill.managed=true",
        "--label", "treadmill.role=worker",
        "--label", "treadmill.family=treadmill-agent",
    ]
    for k, v in env.items():
        cmd.extend(["-e", f"{k}={v}"])
    # Mount the bare-repos directory so the worker can ``git clone file://``.
    bare_root = (Path.cwd() / ".treadmill-local" / "repos").resolve()
    bare_root.mkdir(parents=True, exist_ok=True)
    cmd.extend(["-v", f"{bare_root}:/var/treadmill/repos:rw"])
    # Mount the Claude credentials if present + the real-Claude path is
    # selected. Skip otherwise so the test runner doesn't need it.
    if not dry_run:
        creds = Path.home() / ".claude" / ".credentials.json"
        if creds.exists():
            cmd.extend(["-v", f"{creds}:/root/.claude/.credentials.json:rw"])
    cmd.append(AGENT_IMAGE)

    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    container_id = result.stdout.strip()
    return container_id


def _stop_container(container_id: str) -> None:
    """Best-effort container teardown. ``docker rm -f`` is idempotent;
    if the container already exited cleanly (``EXIT_AFTER_STEP=true``)
    + was launched with ``--rm``, it's already gone."""
    subprocess.run(
        ["docker", "rm", "-f", container_id],
        capture_output=True, check=False,
    )


def _pause_autoscaler() -> bool:
    """Send SIGSTOP to the autoscaler subprocess (if running).

    The autoscaler polls the work queue every ~2s and launches its own
    worker containers; if it picks up the test's claim first, the
    test's manually-started worker has nothing to consume. Pausing
    keeps the autoscaler off the queue without tearing down its state
    file, so the substrate's normal teardown path still finds it.

    Returns True iff the autoscaler was paused (so the test knows to
    resume it on cleanup).
    """
    pid_file = Path.cwd() / ".treadmill-local" / "autoscaler.pid"
    if not pid_file.is_file():
        return False
    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        return False
    try:
        os.kill(pid, signal.SIGSTOP)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _stop_existing_worker_containers() -> None:
    """Force-remove any pre-existing agent worker containers so the test's
    container is the only consumer of the work claim. Idempotent."""
    result = subprocess.run(
        [
            "docker", "ps", "-a", "-q",
            "--filter", "label=treadmill.family=treadmill-agent",
            "--filter", "label=treadmill.role=worker",
        ],
        capture_output=True, text=True, check=False,
    )
    ids = [line for line in result.stdout.splitlines() if line.strip()]
    if not ids:
        return
    subprocess.run(
        ["docker", "rm", "-f", *ids],
        capture_output=True, check=False,
    )


def _resume_autoscaler() -> None:
    """Send SIGCONT to the autoscaler subprocess. Idempotent; safe to
    call even if the autoscaler was never paused."""
    pid_file = Path.cwd() / ".treadmill-local" / "autoscaler.pid"
    if not pid_file.is_file():
        return
    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        return
    try:
        os.kill(pid, signal.SIGCONT)
    except (ProcessLookupError, PermissionError):
        pass




def _container_logs(container_id: str) -> str:
    """Pull container logs for assertion-failure diagnostics."""
    result = subprocess.run(
        ["docker", "logs", container_id],
        capture_output=True, text=True, check=False,
    )
    return f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"


def _wait_for_step_status(
    engine: Engine,
    run_id: str,
    expected_status: str,
    *,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Poll ``workflow_run_steps`` until ``status == expected_status`` or
    timeout. Returns the row dict. Raises ``TimeoutError`` with the most
    recent observed status on failure so the test message has context."""
    deadline = time.monotonic() + timeout
    last_status: str | None = None
    while time.monotonic() < deadline:
        with engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT id, status, output, completed_at "
                    "FROM workflow_run_steps WHERE run_id = :rid"
                ),
                {"rid": run_id},
            ).mappings().one_or_none()
            if row is not None:
                last_status = row["status"]
                if row["status"] == expected_status:
                    return dict(row)
        time.sleep(0.5)
    raise TimeoutError(
        f"workflow_run_steps for run_id={run_id} did not reach "
        f"status={expected_status!r} within {timeout:.1f}s "
        f"(last_status={last_status!r})"
    )


# ── The integration test ──────────────────────────────────────────────────────


def test_worker_container_end_to_end(
    client: httpx.Client,
    engine: Engine,
    boto_kwargs: dict[str, Any],
    bare_repo: tuple[str, Path],
    truncate: None,
) -> None:
    """Full loop: submit a plan, launch a worker container, assert the
    consumer projects ``completed`` + the events table carries the
    three lifecycle rows + no ``task_prs`` row was written (B.7 removed
    the gh PR-opening path, so neither the dry-run nor the real-Claude
    runs produce a real PR)."""
    repo, _bare = bare_repo

    _ensure_starters_seeded(client)
    work_queue_url = _sqs_work_queue_url(boto_kwargs)
    events_topic_arn = _events_topic_arn(boto_kwargs)

    # Pause the autoscaler so it doesn't race the test's manually-
    # started container for the work-queue claim. The autoscaler's
    # containers don't carry the dry-run env (B.9 removed it from CDK)
    # so if it wins the race in dry-run mode the test would observe a
    # real-Claude run instead.
    autoscaler_paused = _pause_autoscaler()
    # Remove any worker containers the autoscaler may have started
    # before the pause took effect. (Force-removing a not-running
    # container is a no-op; a running one gets SIGKILL'd.) This
    # guarantees only the test's container will consume the claim.
    _stop_existing_worker_containers()

    # Submit a Scenario-1 plan with a one-task plan doc that names
    # ``wf-author``. The parser requires the strict-YAML block shape
    # (id / title / workflow / intent / scope / validation). The dev
    # fast-path (intent + dev=true) was an option but its activation
    # depends on the API's resolved ``settings.is_fully_local`` value which is
    # not under this test's control; Scenario 1 is deterministic.
    doc = (
        "# Plan: agent container smoke\n\n"
        "## sequence_of_work\n\n"
        "```yaml\n"
        "sequence_of_work:\n"
        "  - id: smoke\n"
        '    title: "Smoke marker"\n'
        "    workflow: wf-author\n"
        "    intent: append `smoke ok` as a new line to README.md\n"
        "    scope:\n"
        "      files: [README.md]\n"
        "    validation:\n"
        "      - kind: deterministic\n"
        '        description: "README.md gained a line"\n'
        "```\n"
    )
    response = client.post(
        "/api/v1/plans",
        json={
            "repo": repo,
            "doc_path": "docs/plans/smoke.md",
            "doc_content": doc,
            "created_by": "test-integration-container",
        },
    )
    assert response.status_code == 201, response.text
    plan = response.json()
    plan_id = plan["id"]

    # Fetch the spawned task + its run.
    tasks_resp = client.get(f"/api/v1/plans/{plan_id}/tasks")
    assert tasks_resp.status_code == 200, tasks_resp.text
    tasks = tasks_resp.json()
    assert len(tasks) == 1, tasks
    task_id = tasks[0]["id"]

    # The dispatcher created a workflow_run + step. Look them up.
    with engine.connect() as conn:
        run_row = conn.execute(
            sa.text(
                "SELECT id FROM workflow_runs WHERE task_id = :tid"
            ),
            {"tid": task_id},
        ).mappings().one()
    run_id = str(run_row["id"])

    # Launch the worker container. Dry-run by default; real-Claude
    # when TREADMILL_REAL_CLAUDE=1 is set.
    container_id = _run_worker_container(
        work_queue_url=work_queue_url,
        events_topic_arn=events_topic_arn,
        dry_run=not REAL_CLAUDE,
    )
    try:
        try:
            row = _wait_for_step_status(
                engine, run_id, "completed", timeout=120.0,
            )
        except TimeoutError as exc:
            # Pull container logs into the failure message for triage.
            logs = _container_logs(container_id)
            raise AssertionError(f"{exc}\n\n{logs}") from exc

        # Assertions on the projected step state.
        assert row["status"] == "completed", row
        assert row["completed_at"] is not None, row
        # In dry-run the envelope (ADR-0012) carries the summary string,
        # a branch artifact, and commit_sha at top-level. In real-Claude
        # mode it carries the same envelope shape.
        output = row["output"] or {}
        assert isinstance(output, dict), output
        branch_artifacts = [
            a for a in output.get("artifacts", []) if a.get("kind") == "branch"
        ]
        assert branch_artifacts, output
        assert branch_artifacts[0]["value"].startswith("task/"), output
        commit_sha = output.get("commit_sha") or ""
        assert isinstance(commit_sha, str) and len(commit_sha) == 40, output

        # Events table carries the three lifecycle rows on this run.
        with engine.connect() as conn:
            events = conn.execute(
                sa.text(
                    "SELECT entity_type, action FROM events "
                    "WHERE run_id = :rid "
                    "ORDER BY created_at"
                ),
                {"rid": run_id},
            ).mappings().all()
        actions = [(e["entity_type"], e["action"]) for e in events]
        # ``step.ready`` from the dispatcher, then the worker's
        # ``step.started`` + ``step.completed``.
        assert ("step", "ready") in actions, actions
        assert ("step", "started") in actions, actions
        assert ("step", "completed") in actions, actions

        # ``task_prs`` should NOT have a row: B.7 removed the gh PR path,
        # so neither the dry-run nor the real-Claude container opens a
        # remote PR. The consumer's task_prs writer (B.8) only fires
        # when ``output.pr_number`` is present — and it isn't here.
        with engine.connect() as conn:
            pr_rows = conn.execute(
                sa.text("SELECT pr_number FROM task_prs WHERE task_id = :tid"),
                {"tid": task_id},
            ).all()
        assert pr_rows == [], pr_rows
    finally:
        _stop_container(container_id)
        if autoscaler_paused:
            _resume_autoscaler()
