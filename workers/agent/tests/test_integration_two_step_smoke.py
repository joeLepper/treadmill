"""End-to-end two-step workflow smoke (Week-3 plan D.1).

This is the canary that proves the multi-step pipeline works end-to-end
against a live substrate:

  1. The webhook receiver normalizes ``check_run.completed`` with
     ``conclusion=failure`` into ``github.check_run_completed``.
  2. The coordination consumer's trigger evaluator dispatches
     ``wf-ci-fix`` for the matching task.
  3. Step 1 (``role-ci-analyzer``) runs in dry-run mode, emitting a
     synthesized ``task_directive`` in its envelope payload.
  4. The consumer's cross-step dispatch fires ``step.ready`` for step 2.
  5. Step 2 (``role-code-author``) reads the prior step's directive,
     makes the dry-run marker change, commits, and pushes a branch.
  6. The events table carries both step completions; both
     ``workflow_run_steps`` rows are ``completed``; the bare repo has
     the fix branch.

``wf-ci-fix`` is the chosen canary because its analyzer input is the
simplest (a failing check_run) — no PR review threads, no conflict
trees. The two-step shape under test applies identically to
``wf-feedback`` / ``wf-conflict`` per ADR-0015 §"Per-workflow shape
matrix"; once this smoke proves the wiring, those follow by symmetry.

Gating
------

  * ``TREADMILL_INTEGRATION=1`` — opt-in switch. Without it pytest skips.
  * ``TREADMILL_LOCAL_HARNESS=1`` — also opts in to the ``local_substrate``
    fixture so the test brings the substrate up itself (otherwise it
    assumes ``treadmill-local up`` is already running).
  * ``TREADMILL_REAL_CLAUDE=1`` — opt-in for the real-Claude variant.
    The default dry-run variant runs without it.

Mirrors the Week-2 closure container smoke
(``tests/test_integration_container.py``) for substrate setup +
container start + DB poll pattern; that test is the nearest precedent
and crib-points are noted inline.
"""

from __future__ import annotations

import json
import os
import shutil
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
        "set TREADMILL_INTEGRATION=1 and TREADMILL_TEST_DATABASE_URL (a DEDICATED test database) and TREADMILL_TEST_API_URL (a test API instance, never the live one) to run the two-step workflow smoke; "
        "requires `treadmill-local up` (or TREADMILL_LOCAL_HARNESS=1 to "
        "use the bring-up fixture)"
    ),
)


DEFAULT_AWS_ENDPOINT = "http://localhost:5001"

AGENT_IMAGE = "treadmill-agent:dev"
AGENT_NETWORK = "treadmill-local"


# Pull in the bring-up fixture conditionally — same pattern as
# ``test_integration_container.py``.
try:
    from treadmill_local.pytest_harness import local_substrate  # noqa: F401
except ImportError:
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
    is set; otherwise no-op (the substrate is assumed to be running)."""
    if USE_HARNESS and local_substrate is not None:
        request.getfixturevalue("local_substrate")


@pytest.fixture(scope="module", autouse=True)
def migrations_applied(database_url: str, _substrate_up: None) -> None:
    """Apply alembic migrations against the live Postgres."""
    services_api_dir = (
        Path(__file__).resolve().parents[3] / "services" / "api"
    )
    if not (services_api_dir / "alembic.ini").is_file():
        pytest.skip(
            f"could not locate alembic.ini under {services_api_dir}"
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
    """Make sure ``treadmill-agent:dev`` exists locally; build it if not."""
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
    """Seed a unique bare repo under the host-side path the runtime
    mounts into the worker."""
    from treadmill_local.repos import init_bare_repo

    repo = f"treadmill/two-step-smoke-{uuid.uuid4().hex[:8]}"
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
    """Wipe per-test rows; leave the workflows / roles catalog seeded by
    the prior call to ``_ensure_starters_seeded``."""
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
    """Idempotently seed the starter workflows + roles so ``wf-ci-fix``
    + its two roles exist before the trigger evaluator looks them up.

    Mirrors ``test_integration_container.py:_ensure_starters_seeded``;
    the forwarder shim exists to bridge the seed function's
    ``ApiClient`` shape to the test's live httpx client.
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
    sqs = boto3.client("sqs", **boto_kwargs)
    resp = sqs.list_queues()
    urls = resp.get("QueueUrls", [])
    for url in urls:
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
    """Start the agent container with env overrides, kept running across
    both steps (``EXIT_AFTER_STEP=false``).

    Cross-step dispatch fires ``step.ready`` for step 2 only after step
    1 completes — so a single container that long-polls the queue picks
    both claims. The test stops the container in its finally block.
    """
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
        # Two steps in one container — disable the one-shot exit so the
        # second SQS claim (published by cross-step dispatch after step
        # 1 completes) lands with the same worker. The runner still
        # exits on an empty long-poll cycle, so we use a long poll wait
        # (20s) to give cross-step dispatch's SNS→consumer→SQS hop time
        # to land the next claim before the worker would exit.
        "EXIT_AFTER_STEP": "false",
        "POLL_WAIT_SECONDS": "20",
    }
    if dry_run:
        env["TREADMILL_AGENT_DRY_RUN"] = "1"

    name = f"treadmill-test-twostep-{uuid.uuid4().hex[:8]}"
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
    bare_root = (Path.cwd() / ".treadmill-local" / "repos").resolve()
    bare_root.mkdir(parents=True, exist_ok=True)
    cmd.extend(["-v", f"{bare_root}:/var/treadmill/repos:rw"])
    if not dry_run:
        creds = Path.home() / ".claude" / ".credentials.json"
        if creds.exists():
            cmd.extend(["-v", f"{creds}:/root/.claude/.credentials.json:rw"])
    cmd.append(AGENT_IMAGE)

    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    container_id = result.stdout.strip()
    return container_id


def _stop_container(container_id: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", container_id],
        capture_output=True, check=False,
    )


def _pause_autoscaler() -> bool:
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


def _resume_autoscaler() -> None:
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


def _stop_existing_worker_containers() -> None:
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


def _container_logs(container_id: str) -> str:
    result = subprocess.run(
        ["docker", "logs", container_id],
        capture_output=True, text=True, check=False,
    )
    return f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"


def _seed_plan_task_pr(
    *,
    engine: Engine,
    repo: str,
    pr_number: int = 142,
) -> tuple[str, str]:
    """Seed a Plan + Task pointing at ``wf-author`` (the task's pinned
    workflow_version_id is unrelated to the workflow the trigger
    evaluator dispatches per ADR-0015 §"Per-workflow shape matrix").
    Also seed a ``task_prs`` bridge row so the trigger evaluator can
    resolve the task from the check_run_completed event's PR number.

    Returns ``(plan_id, task_id)`` as string UUIDs.
    """
    with engine.begin() as conn:
        # The seed_starters call has already populated wf-author. Look
        # up its workflow_version_id so the task's FK resolves.
        wv_id = conn.execute(sa.text(
            "SELECT id FROM workflow_versions "
            "WHERE workflow_id = 'wf-author' "
            "ORDER BY version DESC LIMIT 1"
        )).scalar()
        assert wv_id is not None, (
            "wf-author has no workflow_version row; seed_starters should "
            "have populated it. Did the seed step succeed?"
        )
        plan_id = conn.execute(sa.text(
            "INSERT INTO plans (repo) VALUES (:repo) RETURNING id"
        ), {"repo": repo}).scalar()
        # Plans are gated on having an `activated` event before dispatch
        # is allowed (ADR-0011's plan_active gate). Insert one directly
        # so the trigger evaluator's run materializes cleanly.
        conn.execute(sa.text(
            "INSERT INTO events (entity_type, action, plan_id, payload) "
            "VALUES ('plan', 'activated', :p, '{}'::jsonb)"
        ), {"p": plan_id})
        task_id = conn.execute(sa.text(
            "INSERT INTO tasks (plan_id, repo, title, workflow_version_id) "
            "VALUES (:p, :repo, 'two-step-smoke', :wv) RETURNING id"
        ), {"p": plan_id, "wv": wv_id, "repo": repo}).scalar()
        conn.execute(sa.text(
            "INSERT INTO task_prs (repo, pr_number, task_id, branch) "
            "VALUES (:repo, :pr, :t, 'task/foo')"
        ), {"repo": repo, "pr": pr_number, "t": task_id})

    return str(plan_id), str(task_id)


def _post_check_run_failure(
    *,
    client: httpx.Client,
    repo: str,
    pr_number: int,
    head_sha: str = "deadbeef" * 5,
    check_name: str = "ci/tests",
) -> str:
    """POST a ``check_run.completed`` webhook with ``conclusion=failure``.

    Mirrors the GitHub webhook body shape; the receiver normalizes,
    persists the Event, and the consumer's trigger evaluator fans out to
    ``wf-ci-fix``. Returns the event_id from the response.
    """
    body = {
        "action": "completed",
        "check_run": {
            "name": check_name,
            "conclusion": "failure",
            "head_sha": head_sha,
            "pull_requests": [{"number": pr_number}],
        },
        "repository": {"full_name": repo},
    }
    response = client.post(
        "/api/v1/webhooks/github",
        content=json.dumps(body),
        headers={
            "X-GitHub-Event": "check_run",
            "X-GitHub-Delivery": f"test-delivery-{uuid.uuid4().hex[:8]}",
        },
    )
    assert response.status_code == 202, response.text
    rj = response.json()
    assert rj["action"] == "check_run_completed", rj
    return rj["event_id"]


def _wait_for_first_step_ready(
    engine: Engine,
    task_id: str,
    *,
    timeout: float = 15.0,
) -> str:
    """Poll until the trigger evaluator has materialized the wf-ci-fix
    run for ``task_id`` and published its first ``step.ready`` event.

    Returns the run_id once the step.ready event lands. Raises
    TimeoutError otherwise — that tells us the consumer's trigger
    evaluator branch never fired (which is a different failure than
    the worker hanging).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as conn:
            row = conn.execute(sa.text(
                "SELECT wr.id "
                "FROM workflow_runs wr "
                "JOIN workflow_versions wv ON wv.id = wr.workflow_version_id "
                "JOIN events e ON e.run_id = wr.id "
                "WHERE wr.task_id = :tid AND wv.workflow_id = 'wf-ci-fix' "
                "AND e.entity_type = 'step' AND e.action = 'ready' "
                "LIMIT 1"
            ), {"tid": task_id}).first()
            if row is not None:
                return str(row.id)
        time.sleep(0.5)
    raise TimeoutError(
        f"wf-ci-fix run for task_id={task_id} did not produce a "
        f"step.ready event within {timeout:.1f}s; the trigger "
        f"evaluator's dispatch never fired (check API + consumer logs)"
    )


def _wait_for_run_completion(
    engine: Engine,
    task_id: str,
    workflow_id: str = "wf-ci-fix",
    *,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Poll until the ``wf-ci-fix`` run for ``task_id`` has all its
    workflow_run_steps at ``status='completed'`` (or timeout).

    Returns ``{"run_id": ..., "steps": [...]}`` on success. Raises
    ``TimeoutError`` with last-observed-states on failure.
    """
    deadline = time.monotonic() + timeout
    last_observed: list[tuple[int, str]] = []
    while time.monotonic() < deadline:
        with engine.connect() as conn:
            run_row = conn.execute(sa.text(
                "SELECT wr.id "
                "FROM workflow_runs wr "
                "JOIN workflow_versions wv ON wv.id = wr.workflow_version_id "
                "WHERE wr.task_id = :tid AND wv.workflow_id = :wid "
                "ORDER BY wr.created_at DESC LIMIT 1"
            ), {"tid": task_id, "wid": workflow_id}).first()
            if run_row is None:
                time.sleep(0.5)
                continue
            run_id = run_row.id
            steps = conn.execute(sa.text(
                "SELECT step_index, status, output "
                "FROM workflow_run_steps "
                "WHERE run_id = :rid "
                "ORDER BY step_index"
            ), {"rid": run_id}).mappings().all()
            last_observed = [(s["step_index"], s["status"]) for s in steps]
            if (
                len(steps) == 2
                and all(s["status"] == "completed" for s in steps)
            ):
                return {"run_id": str(run_id), "steps": [dict(s) for s in steps]}
        time.sleep(0.5)
    raise TimeoutError(
        f"wf-ci-fix run for task_id={task_id} did not reach "
        f"all-completed within {timeout:.1f}s "
        f"(last_observed={last_observed!r})"
    )


def _branch_exists_in_bare(bare: Path, prefix: str = "task/") -> str | None:
    """Return the first branch name under the bare repo whose name
    starts with ``prefix``, or ``None`` if no such branch exists.

    The worker's ``_branch_for_step`` produces ``task/<short-id>-<slug>``;
    we don't predict the exact name here (the slug depends on the task
    title and the short id is a substring), so we match by prefix.
    """
    result = subprocess.run(
        ["git", "-C", str(bare), "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        capture_output=True, text=True, check=True,
    )
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            return line
    return None


# ── The integration tests ────────────────────────────────────────────────────


def test_two_step_workflow_dry_run(
    client: httpx.Client,
    engine: Engine,
    boto_kwargs: dict[str, Any],
    bare_repo: tuple[str, Path],
    truncate: None,
) -> None:
    """Drive a check_run.completed failure through the full pipeline in
    dry-run mode. The worker writes a marker file per step (no LLM
    invoked); both step completions land + the bare repo has a
    task-branch with the analyzer's directive-named file in it.

    The dry-run analyzer extension (runner.py ``_dry_run_task_directive``)
    is what makes this exercisable without real Claude — its synthesized
    ``task_directive`` lands in the analyzer's StepOutput payload, and
    the cross-step dispatch's commit_sha plumbing carries through to
    step 2's claim.
    """
    repo, bare = bare_repo

    _ensure_starters_seeded(client)
    work_queue_url = _sqs_work_queue_url(boto_kwargs)
    events_topic_arn = _events_topic_arn(boto_kwargs)

    autoscaler_paused = _pause_autoscaler()
    _stop_existing_worker_containers()

    _plan_id, task_id = _seed_plan_task_pr(
        engine=engine, repo=repo, pr_number=142,
    )

    # Fire the webhook FIRST so the trigger evaluator publishes the
    # step-1 SQS claim before the worker starts polling. The runner
    # exits on an empty long-poll cycle, so without a pre-existing
    # claim the worker would exit immediately. Cross-step dispatch
    # publishes step-2's claim near-instantly after step 1 completes,
    # which lands within the 20s long-poll window the worker uses.
    _post_check_run_failure(
        client=client, repo=repo, pr_number=142,
    )
    # Give the consumer a beat to project the github event + materialize
    # the wf-ci-fix run (with its first step claim in SQS) before the
    # worker container starts polling.
    _wait_for_first_step_ready(engine, task_id, timeout=15.0)

    container_id = _run_worker_container(
        work_queue_url=work_queue_url,
        events_topic_arn=events_topic_arn,
        dry_run=True,
    )
    try:
        # Poll until both steps complete. 120s caps total wait.
        try:
            result = _wait_for_run_completion(
                engine, task_id, "wf-ci-fix", timeout=120.0,
            )
        except TimeoutError as exc:
            logs = _container_logs(container_id)
            raise AssertionError(f"{exc}\n\n{logs}") from exc

        run_id = result["run_id"]
        steps = result["steps"]

        # Both steps completed.
        assert len(steps) == 2, steps
        assert steps[0]["status"] == "completed", steps
        assert steps[1]["status"] == "completed", steps

        # Step 1 (analyzer) emitted the dry-run task_directive in its
        # envelope payload. This is the cross-step handoff contract per
        # ADR-0015 §"task_directive — the analyzer-action contract".
        step1_output = steps[0]["output"] or {}
        assert isinstance(step1_output, dict), step1_output
        payload = step1_output.get("payload") or {}
        assert "task_directive" in payload, (
            f"analyzer step's payload missing task_directive; got "
            f"payload={payload!r}. Did _dry_run_task_directive run?"
        )
        directive = payload["task_directive"]
        assert isinstance(directive.get("intent"), str) and directive["intent"]
        assert isinstance(directive.get("files"), list) and directive["files"]

        # Step 2 (action) also pushed a branch + has commit_sha.
        step2_output = steps[1]["output"] or {}
        assert step2_output.get("commit_sha"), step2_output
        assert len(step2_output["commit_sha"]) == 40, step2_output

        # Events table has the four lifecycle rows (step.started x2 +
        # step.completed x2) plus the dispatcher's step.ready rows.
        with engine.connect() as conn:
            events = conn.execute(sa.text(
                "SELECT entity_type, action, step_id "
                "FROM events "
                "WHERE run_id = :rid "
                "ORDER BY created_at"
            ), {"rid": run_id}).mappings().all()
        actions = [(e["entity_type"], e["action"]) for e in events]
        # Two step.ready rows — one from the trigger evaluator (step 1)
        # and one from the cross-step dispatch (step 2).
        assert actions.count(("step", "ready")) == 2, actions
        assert actions.count(("step", "started")) == 2, actions
        assert actions.count(("step", "completed")) == 2, actions

        # Bare repo received at least one task/* branch — the action
        # step pushed it. The exact name is task/<short>-two-step-smoke
        # but we match by prefix because the short id depends on the
        # task id allocation.
        branch = _branch_exists_in_bare(bare, prefix="task/")
        assert branch is not None, (
            f"bare repo at {bare} has no task/* branch; the action step "
            f"should have pushed one. Container logs:\n"
            f"{_container_logs(container_id)}"
        )
    finally:
        _stop_container(container_id)
        if autoscaler_paused:
            _resume_autoscaler()


@pytest.mark.skipif(
    not REAL_CLAUDE,
    reason=(
        "set TREADMILL_REAL_CLAUDE=1 to run the real-Claude variant; this "
        "test issues two billable LLM calls (~$0.002 per run on haiku)"
    ),
)
@pytest.mark.skipif(
    not (Path.home() / ".claude" / ".credentials.json").exists(),
    reason="~/.claude/.credentials.json not found",
)
@pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="docker is required to start the worker container",
)
def test_two_step_workflow_real_claude(
    client: httpx.Client,
    engine: Engine,
    boto_kwargs: dict[str, Any],
    bare_repo: tuple[str, Path],
    truncate: None,
) -> None:
    """Real-Claude variant of the two-step smoke. The analyzer
    (``role-ci-analyzer``) sees a hand-seeded fake CI failure log and
    produces a real ``task_directive``; the action role
    (``role-code-author``) reads it and pushes a fix.

    Cost: two haiku-class LLM calls per run, ~$0.002 total. Opt-in
    only; the default integration suite uses the dry-run path.

    The shape of assertions mirrors the dry-run variant; only the
    runtime path differs (no ``TREADMILL_AGENT_DRY_RUN``). The
    role-code-author smoke (``test_integration_real_claude.py``) covers
    the action-role end of the pipeline at the unit-runner level; this
    test covers the full e2e against the live substrate.
    """
    repo, bare = bare_repo

    _ensure_starters_seeded(client)
    work_queue_url = _sqs_work_queue_url(boto_kwargs)
    events_topic_arn = _events_topic_arn(boto_kwargs)

    autoscaler_paused = _pause_autoscaler()
    _stop_existing_worker_containers()

    _plan_id, task_id = _seed_plan_task_pr(
        engine=engine, repo=repo, pr_number=142,
    )

    # Fire the webhook before starting the worker so SQS already has the
    # step-1 claim when the worker first polls. Same reasoning as the
    # dry-run variant — the runner exits on an empty long-poll cycle.
    _post_check_run_failure(
        client=client, repo=repo, pr_number=142,
        check_name="ci/lint",
    )
    _wait_for_first_step_ready(engine, task_id, timeout=15.0)

    container_id = _run_worker_container(
        work_queue_url=work_queue_url,
        events_topic_arn=events_topic_arn,
        dry_run=False,
    )
    try:
        try:
            result = _wait_for_run_completion(
                engine, task_id, "wf-ci-fix",
                # Real Claude calls take longer; bump the cap.
                timeout=300.0,
            )
        except TimeoutError as exc:
            logs = _container_logs(container_id)
            raise AssertionError(f"{exc}\n\n{logs}") from exc

        steps = result["steps"]
        assert len(steps) == 2, steps
        assert all(s["status"] == "completed" for s in steps), steps

        # The action step pushed a branch. We don't assert on the
        # directive contents here (the analyzer's free-form output isn't
        # deterministic); the existence of a branch is the contract.
        branch = _branch_exists_in_bare(bare, prefix="task/")
        assert branch is not None, (
            f"bare repo at {bare} has no task/* branch after real-Claude "
            f"two-step run; container logs:\n{_container_logs(container_id)}"
        )
    finally:
        _stop_container(container_id)
        if autoscaler_paused:
            _resume_autoscaler()
