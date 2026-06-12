"""Integration tests for the webhooks router against the live API.

Skipped by default; opt in with ``TREADMILL_INTEGRATION=1``.

These tests cover end-to-end signature verification + normalization +
persistence. The bus publish is a log-stub at v0; no integration test
asserts publication directly. A future day adds SNS-backed publishing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
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
# Task 3aaba5e7: NO live-API default. TREADMILL_API_URL is ambient in
# every team-session env (it points at the LIVE deployment), so a
# fallback to it — or to localhost:8088, which IS the live stack on
# the operator host — silently sends this file's writes to
# production state. A DEDICATED test var makes that an explicit act,
# mirroring TREADMILL_TEST_DATABASE_URL.
TEST_API_URL = os.environ.get("TREADMILL_TEST_API_URL")
pytestmark = pytest.mark.skipif(
    not (INTEGRATION and TEST_DB_URL and TEST_API_URL),
    reason="set TREADMILL_INTEGRATION=1 and TREADMILL_TEST_DATABASE_URL (a DEDICATED test database) and TREADMILL_TEST_API_URL (a test API instance, never the live one) to run; requires `treadmill-local up`",
)




@pytest.fixture(scope="module")
def api_url() -> str:
    return TEST_API_URL


@pytest.fixture(scope="module")
def database_url() -> str:
    return TEST_DB_URL


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


_TEST_TABLES = (
    "plans", "tasks", "task_prs", "task_dependencies",
    "workflow_runs", "workflow_run_steps", "events",
    "event_triggers",
    "workflows", "workflow_versions", "workflow_version_steps",
    "roles", "skills", "hooks",
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


# ── Helpers ──────────────────────────────────────────────────────────────────


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _pr_opened_body(*, repo: str = "test/repo", pr_number: int = 42) -> dict:
    return {
        "action": "opened",
        "pull_request": {
            "number": pr_number,
            "title": "Add billing page",
            "merged": False,
            "head": {"ref": "task/abc-billing", "sha": "deadbeef" * 5},
        },
        "repository": {"full_name": repo},
        "sender": {"login": "alice"},
    }


def _pr_merged_body(*, repo: str = "test/repo", pr_number: int = 42) -> dict:
    return {
        "action": "closed",
        "pull_request": {
            "number": pr_number,
            "merged": True,
            "merge_commit_sha": "cafebabe" * 5,
            "head": {"sha": "deadbeef" * 5},
        },
        "repository": {"full_name": repo},
        "sender": {"login": "alice"},
    }


def _pr_synchronize_body(
    *,
    repo: str = "test/repo",
    pr_number: int = 42,
    head_sha: str = "feedface" * 5,
    before: str | None = "deadbeef" * 5,
) -> dict:
    body = {
        "action": "synchronize",
        "pull_request": {
            "number": pr_number,
            "head": {"ref": "task/abc-billing", "sha": head_sha},
        },
        "repository": {"full_name": repo},
        "sender": {"login": "alice"},
    }
    if before is not None:
        body["before"] = before
    return body


# ── Signature verification (end-to-end) ──────────────────────────────────────
#
# The API container is started without GITHUB_WEBHOOK_SECRET set, so signature
# verification is in dev-mode short-circuit. The signature-verification logic
# itself is exhaustively tested in unit tests; the integration tests here
# focus on the end-to-end happy path and the HTTP-level error shapes.


def test_post_without_x_github_event_header_returns_400(
    client: httpx.Client, truncate: None
) -> None:
    response = client.post("/api/v1/webhooks/github", content="{}")
    assert response.status_code == 400
    assert "X-GitHub-Event" in response.json()["detail"]


def test_post_with_invalid_json_returns_400(client: httpx.Client, truncate: None) -> None:
    response = client.post(
        "/api/v1/webhooks/github",
        content="not-json{",
        headers={"X-GitHub-Event": "ping"},
    )
    # Ping is unhandled (skipped), but body parsing happens first; invalid JSON
    # short-circuits with 400.
    assert response.status_code == 400
    assert "JSON" in response.json()["detail"]


# ── Normalization paths ──────────────────────────────────────────────────────


def test_pr_opened_persists_event_with_no_task_id_when_no_bridge_row(
    client: httpx.Client, truncate: None, engine: Engine
) -> None:
    body = _pr_opened_body()
    response = client.post(
        "/api/v1/webhooks/github",
        content=json.dumps(body),
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "test-delivery-1",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 202, response.text
    body_resp = response.json()
    assert body_resp["status"] == "accepted"
    assert body_resp["entity_type"] == "github"
    assert body_resp["action"] == "pr_opened"
    assert body_resp["task_id"] is None  # no task_prs row yet
    assert body_resp["delivery"] == "test-delivery-1"

    # Confirm Event row exists in DB.
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT entity_type, action, task_id, payload "
                "FROM events WHERE id = :id"
            ),
            {"id": body_resp["event_id"]},
        ).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.entity_type == "github"
    assert row.action == "pr_opened"
    assert row.task_id is None
    assert row.payload["pr_number"] == 42
    assert row.payload["title"] == "Add billing page"


def test_pr_opened_resolves_task_id_when_bridge_row_exists(
    client: httpx.Client, truncate: None, engine: Engine
) -> None:
    """When task_prs has (repo, pr_number) → task_id, the persisted Event
    carries that task_id."""
    # Seed: a Plan + Task + a task_prs bridge row.
    with engine.begin() as conn:
        # Need workflow + role for the task FK.
        conn.execute(sa.text("INSERT INTO workflows (id) VALUES ('wf-author')"))
        wv_id = conn.execute(
            sa.text(
                "INSERT INTO workflow_versions (workflow_id, version) "
                "VALUES ('wf-author', 1) RETURNING id"
            )
        ).scalar()
        plan_id = conn.execute(
            sa.text("INSERT INTO plans (repo) VALUES ('test/repo') RETURNING id")
        ).scalar()
        task_id = conn.execute(
            sa.text(
                "INSERT INTO tasks (plan_id, repo, title, workflow_version_id) "
                "VALUES (:p, 'test/repo', 'T', :wv) RETURNING id"
            ),
            {"p": plan_id, "wv": wv_id},
        ).scalar()
        conn.execute(
            sa.text(
                "INSERT INTO task_prs (repo, pr_number, task_id) "
                "VALUES ('test/repo', 42, :t)"
            ),
            {"t": task_id},
        )

    response = client.post(
        "/api/v1/webhooks/github",
        content=json.dumps(_pr_opened_body()),
        headers={"X-GitHub-Event": "pull_request"},
    )
    assert response.status_code == 202
    assert response.json()["task_id"] == str(task_id)


def test_pr_merged_persists_event(client: httpx.Client, truncate: None, engine: Engine) -> None:
    body = _pr_merged_body()
    response = client.post(
        "/api/v1/webhooks/github",
        content=json.dumps(body),
        headers={"X-GitHub-Event": "pull_request"},
    )
    assert response.status_code == 202
    assert response.json()["action"] == "pr_merged"

    with engine.connect() as conn:
        ev = conn.execute(
            sa.text("SELECT payload FROM events WHERE action = 'pr_merged'")
        ).scalar()
    assert ev["merged_sha"] == "cafebabe" * 5


def test_pr_closed_without_merged_is_skipped(client: httpx.Client, truncate: None) -> None:
    body = {
        "action": "closed",
        "pull_request": {"number": 42, "merged": False, "head": {}},
        "repository": {"full_name": "test/repo"},
        "sender": {"login": "alice"},
    }
    response = client.post(
        "/api/v1/webhooks/github",
        content=json.dumps(body),
        headers={"X-GitHub-Event": "pull_request"},
    )
    assert response.status_code == 202
    assert response.json()["status"] == "skipped"


def test_unhandled_event_type_is_skipped(client: httpx.Client, truncate: None) -> None:
    response = client.post(
        "/api/v1/webhooks/github",
        content=json.dumps({"zen": "Speak like a human."}),
        headers={"X-GitHub-Event": "ping"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "skipped"
    assert "ping" in body["reason"]


def test_review_submitted_persists(client: httpx.Client, truncate: None, engine: Engine) -> None:
    body = {
        "action": "submitted",
        "review": {"state": "approved", "body": "lgtm"},
        "pull_request": {"number": 42},
        "repository": {"full_name": "test/repo"},
        "sender": {"login": "bob"},
    }
    response = client.post(
        "/api/v1/webhooks/github",
        content=json.dumps(body),
        headers={"X-GitHub-Event": "pull_request_review"},
    )
    assert response.status_code == 202
    assert response.json()["action"] == "pr_review_submitted"


def test_check_run_completed_persists(client: httpx.Client, truncate: None) -> None:
    body = {
        "action": "completed",
        "check_run": {
            "name": "ci",
            "conclusion": "failure",
            "head_sha": "deadbeef" * 5,
            "pull_requests": [{"number": 42}],
        },
        "repository": {"full_name": "test/repo"},
    }
    response = client.post(
        "/api/v1/webhooks/github",
        content=json.dumps(body),
        headers={"X-GitHub-Event": "check_run"},
    )
    assert response.status_code == 202
    assert response.json()["action"] == "check_run_completed"


# ── commit_sha plumbing (ADR-0014) ───────────────────────────────────────────
#
# Every github event whose semantics include "I ran against a specific HEAD"
# populates ``events.commit_sha`` so ADR-0013's mergeability VIEW can join on
# it without JSONB extraction. The receiver pulls the SHA from the raw GitHub
# payload at insert time; the column is *never* re-derived after the fact.


def test_pr_opened_populates_commit_sha(
    client: httpx.Client, truncate: None, engine: Engine
) -> None:
    body = _pr_opened_body()
    response = client.post(
        "/api/v1/webhooks/github",
        content=json.dumps(body),
        headers={"X-GitHub-Event": "pull_request"},
    )
    assert response.status_code == 202
    event_id = response.json()["event_id"]
    with engine.connect() as conn:
        commit_sha = conn.execute(
            sa.text("SELECT commit_sha FROM events WHERE id = :id"),
            {"id": event_id},
        ).scalar()
    assert commit_sha == "deadbeef" * 5


def test_pr_synchronize_normalizes_and_populates_commit_sha(
    client: httpx.Client, truncate: None, engine: Engine
) -> None:
    """End-to-end: a ``pull_request.synchronize`` webhook lands as a
    ``github.pr_synchronize`` Event row with ``commit_sha`` populated
    from the new HEAD."""
    body = _pr_synchronize_body(head_sha="cafebabe" * 5)
    response = client.post(
        "/api/v1/webhooks/github",
        content=json.dumps(body),
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "test-delivery-sync",
        },
    )
    assert response.status_code == 202, response.text
    body_resp = response.json()
    assert body_resp["status"] == "accepted"
    assert body_resp["entity_type"] == "github"
    assert body_resp["action"] == "pr_synchronize"

    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT entity_type, action, commit_sha, payload "
                "FROM events WHERE id = :id"
            ),
            {"id": body_resp["event_id"]},
        ).one()
    assert row.entity_type == "github"
    assert row.action == "pr_synchronize"
    assert row.commit_sha == "cafebabe" * 5
    assert row.payload["head_sha"] == "cafebabe" * 5
    assert row.payload["before_sha"] == "deadbeef" * 5
    assert row.payload["pr_number"] == 42


def test_pr_review_submitted_populates_commit_sha_from_review_commit_id(
    client: httpx.Client, truncate: None, engine: Engine
) -> None:
    """``pr_review_submitted`` carries the SHA the reviewer reviewed via
    ``review.commit_id`` — the receiver pulls it onto the Event row."""
    body = {
        "action": "submitted",
        "review": {
            "state": "approved",
            "body": "lgtm",
            "commit_id": "deadbeef" * 5,
        },
        "pull_request": {"number": 42},
        "repository": {"full_name": "test/repo"},
        "sender": {"login": "bob"},
    }
    response = client.post(
        "/api/v1/webhooks/github",
        content=json.dumps(body),
        headers={"X-GitHub-Event": "pull_request_review"},
    )
    assert response.status_code == 202
    event_id = response.json()["event_id"]
    with engine.connect() as conn:
        commit_sha = conn.execute(
            sa.text("SELECT commit_sha FROM events WHERE id = :id"),
            {"id": event_id},
        ).scalar()
    assert commit_sha == "deadbeef" * 5


def test_check_run_completed_populates_commit_sha(
    client: httpx.Client, truncate: None, engine: Engine
) -> None:
    body = {
        "action": "completed",
        "check_run": {
            "name": "ci",
            "conclusion": "failure",
            "head_sha": "cafebabe" * 5,
            "pull_requests": [{"number": 42}],
        },
        "repository": {"full_name": "test/repo"},
    }
    response = client.post(
        "/api/v1/webhooks/github",
        content=json.dumps(body),
        headers={"X-GitHub-Event": "check_run"},
    )
    assert response.status_code == 202
    event_id = response.json()["event_id"]
    with engine.connect() as conn:
        commit_sha = conn.execute(
            sa.text("SELECT commit_sha FROM events WHERE id = :id"),
            {"id": event_id},
        ).scalar()
    assert commit_sha == "cafebabe" * 5


def test_pr_merged_populates_commit_sha_from_merge_commit_sha(
    client: httpx.Client, truncate: None, engine: Engine
) -> None:
    """``pr_merged`` prefers the merge commit SHA over the PR head SHA."""
    body = _pr_merged_body()
    response = client.post(
        "/api/v1/webhooks/github",
        content=json.dumps(body),
        headers={"X-GitHub-Event": "pull_request"},
    )
    assert response.status_code == 202
    event_id = response.json()["event_id"]
    with engine.connect() as conn:
        commit_sha = conn.execute(
            sa.text("SELECT commit_sha FROM events WHERE id = :id"),
            {"id": event_id},
        ).scalar()
    assert commit_sha == "cafebabe" * 5


def test_pr_merged_falls_back_to_head_sha_when_merge_commit_sha_absent(
    client: httpx.Client, truncate: None, engine: Engine
) -> None:
    """If GitHub's payload lacks ``merge_commit_sha`` (defensive), the
    receiver falls back to ``pull_request.head.sha``."""
    body = {
        "action": "closed",
        "pull_request": {
            "number": 42,
            "merged": True,
            # No merge_commit_sha set.
            "head": {"sha": "feedface" * 5},
        },
        "repository": {"full_name": "test/repo"},
        "sender": {"login": "alice"},
    }
    response = client.post(
        "/api/v1/webhooks/github",
        content=json.dumps(body),
        headers={"X-GitHub-Event": "pull_request"},
    )
    assert response.status_code == 202
    event_id = response.json()["event_id"]
    with engine.connect() as conn:
        commit_sha = conn.execute(
            sa.text("SELECT commit_sha FROM events WHERE id = :id"),
            {"id": event_id},
        ).scalar()
    assert commit_sha == "feedface" * 5
