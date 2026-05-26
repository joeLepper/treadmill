"""Real-Claude opt-in smoke (Phase 4 B.11).

The Week 2 close-out claimed an end-to-end smoke passed, but the
``TREADMILL_AGENT_DRY_RUN=1`` env was active — the worker wrote a
deterministic ``.treadmill/<step_id>.md`` marker file instead of
authoring real code. Phase 2 hardened the worker (B.2 drops
``--allow-empty``, B.5 mounts credentials RW, B.6 pins the CLI). Phase
3 removed ``TREADMILL_AGENT_DRY_RUN=1`` from the CDK env (B.9). This
test is the one that proves the real-Claude path actually authors a
change and commits it.

Gating
------

Two env vars must be set for the test to run:

  * ``TREADMILL_REAL_CLAUDE=1`` — opt-in switch (per closure decision
    #7). CI does not flip this by default; budget plumbing arrives in a
    later phase before we change that.

  * ``~/.claude/.credentials.json`` must exist on the host. The Claude
    Code CLI authenticates against the user's subscription via that
    file; without it the binary can't make a call.

Cost
----

A single Haiku call with a trivial prompt is approximately $0.001
(input + output of a few hundred tokens at the
``claude-haiku-4-5-20251001`` rate). The test caps ``timeout_seconds``
at 120 so a hung Claude call (or an authentication failure that takes
a while to surface) doesn't burn budget.

What this test asserts
----------------------

The runner's ``_execute`` function — bypassing the SQS-claim layer — is
driven end-to-end against a real bare repo with a real Claude binary:

  1. The Claude binary was actually invoked (verified by the
     ``summary`` field — the dry-run summary text would have a
     well-known shape, and real Claude returns whatever it wrote).
  2. ``commit_all`` returned a 40-char hex sha (a real commit landed).
  3. The branch landed in the bare repo (``git branch --list``).
  4. The working tree saw at least one file change (we re-clone the
     bare and inspect the branch).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from treadmill_agent import runner
from treadmill_agent.api_client import Role, WorkerContext
from treadmill_agent.config import Settings

from tests.conftest import init_bare_repo


REAL_CLAUDE_GATE = os.environ.get("TREADMILL_REAL_CLAUDE") == "1"
HOST_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"


pytestmark = [
    pytest.mark.skipif(
        not REAL_CLAUDE_GATE,
        reason=(
            "set TREADMILL_REAL_CLAUDE=1 to run the real-Claude smoke; "
            "this test makes a billable LLM call (~$0.001) per invocation"
        ),
    ),
    pytest.mark.skipif(
        not HOST_CREDENTIALS.exists(),
        reason=(
            f"{HOST_CREDENTIALS} not found; the real-Claude smoke needs the "
            "user's Claude subscription credentials on the host. Log into "
            "Claude Code first (or run on a developer machine with an "
            "active session)."
        ),
    ),
    pytest.mark.skipif(
        shutil.which("claude") is None,
        reason="`claude` binary not on PATH; install @anthropic-ai/claude-code",
    ),
]


# Cheapest model — kept in sync with the worker role in
# ``services/api/treadmill_api/starters.py`` (WORKER_MODEL).
_CHEAP_MODEL = "claude-haiku-4-5-20251001"


def _ctx(
    *,
    repo: str,
    task_id: str,
    step_id: str,
) -> WorkerContext:
    """Build a minimal WorkerContext for the real-Claude smoke.

    The role's system prompt + the task description steer Claude toward
    a single trivial file edit so the call stays under a second of
    output and well under our cost cap.
    """
    return WorkerContext(
        step_id=step_id,
        run_id=str(uuid.uuid4()),
        step_index=0,
        step_name="author",
        status="pending",
        task_id=task_id,
        plan_id=str(uuid.uuid4()),
        repo=repo,
        title="Add a smoke marker",
        description=(
            "Append the literal line `smoke ok` as a new line to "
            "`README.md`. Do not modify any other file. Keep the change "
            "to a single line addition."
        ),
        plan_intent="Verify the real-Claude author path lands a change.",
        plan_doc_path=None,
        workflow_id="wf-author",
        workflow_version=1,
        trigger="registered",
        role=Role(
            id="role-author",
            model=_CHEAP_MODEL,
            system_prompt=(
                "You are working in a fresh repo. Add exactly one line. "
                "Be terse — no commentary."
            ),
            output_kind="code",
            skills=[],
            hooks=[],
        ),
        pr_number=None,
        prior_steps=[],
    )


def _settings(bare_repos_dir: Path, workspace_dir: Path) -> Settings:
    """Settings shaped for the local-mode bare-repo path with the
    real-Claude smoke gated. ``events_topic_arn`` is None so the
    publisher noop-drops events (this test does not exercise SNS)."""
    return Settings(
        api_url="http://unused-in-direct-execute",
        work_queue_url="http://unused-in-direct-execute",
        events_topic_arn=None,
        aws_endpoint_url=None,
        aws_region="us-east-1",
        repo_mode="local",
        bare_repos_dir=str(bare_repos_dir),
        workspace_dir=str(workspace_dir),
        exit_after_step=True,
        poll_wait_seconds=1,
        claude_credentials_path=str(HOST_CREDENTIALS),
    )


def test_real_claude_authors_a_change_and_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: real Claude is invoked, makes a file change, the
    runner commits + pushes, and the bare repo sees the branch.

    Cost cap: this test runs Claude Code with ``timeout_seconds=120``
    (set inside the runner via the default in ``claude_code.py``; we
    do not override here because the runner currently bakes the timeout
    in). A trivial-prompt Haiku call completes in well under a minute
    when credentials are live. If credentials are stale the binary will
    surface auth errors quickly via stderr.
    """
    # Hard-disable dry-run so the runner takes the real-Claude path.
    monkeypatch.delenv("TREADMILL_AGENT_DRY_RUN", raising=False)

    bare_repos_dir = tmp_path / "bare"
    bare_repos_dir.mkdir()
    workspace_dir = tmp_path / "ws"
    workspace_dir.mkdir()

    repo = "treadmill/real-claude-smoke"
    bare = init_bare_repo(bare_repos_dir, repo)

    task_id = uuid.uuid4().hex
    step_id = uuid.uuid4().hex

    ctx = _ctx(repo=repo, task_id=task_id, step_id=step_id)
    settings = _settings(bare_repos_dir, workspace_dir)

    output, _ = runner._execute(ctx, settings)

    # ── Assertions on the runner's return shape ──────────────────────

    # Branch matches ADR-0010's ``task/<short-id>-<slug>`` format.
    expected_short = task_id.replace("-", "")[:8]
    assert output["branch"].startswith(f"task/{expected_short}-"), output
    # 40-char hex SHA — git's full SHA1 form. This is the load-bearing
    # proof that ``git.commit_all`` did not raise (B.2: empty commits
    # are not allowed); a real commit landed because Claude wrote
    # changes.
    commit_sha = output["commit_sha"]
    assert isinstance(commit_sha, str), output
    assert len(commit_sha) == 40, commit_sha
    int(commit_sha, 16)  # raises ValueError if not hex.
    # ``pr_number`` / ``pr_url`` are None in local mode.
    assert output["pr_number"] is None, output
    assert output["pr_url"] is None, output
    # The summary is whatever Claude returned. The dry-run path would
    # have produced a deterministic ``dry-run: wrote .treadmill/...``
    # string; the real path returns Claude's own output (which may be
    # any non-empty string). The test asserts non-empty + non-dry-run.
    summary = output["summary"]
    assert isinstance(summary, str) and summary, output
    assert not summary.startswith("dry-run:"), summary

    # ── Assertions on the bare repo state ────────────────────────────

    branch_list = subprocess.run(
        ["git", "-C", str(bare), "branch", "--list", output["branch"]],
        capture_output=True, text=True, check=True,
    )
    assert output["branch"] in branch_list.stdout, branch_list.stdout

    # Re-clone the bare repo to inspect the new branch's tip. The
    # worker's working tree was torn down by the workspace context
    # manager; the bare is the source of truth.
    verify_dir = tmp_path / "verify"
    subprocess.run(
        ["git", "clone", "--branch", output["branch"], str(bare), str(verify_dir)],
        check=True, capture_output=True,
    )
    # The branch tip's commit is the one we just authored.
    head_sha = subprocess.run(
        ["git", "-C", str(verify_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head_sha == commit_sha, (head_sha, commit_sha)

    # Some file change must have landed. Compare the branch tip to
    # ``main`` (the seed commit). The diff must be non-empty.
    diff = subprocess.run(
        ["git", "-C", str(verify_dir), "diff", "--name-only", "origin/main"],
        capture_output=True, text=True, check=True,
    )
    changed_files = [line for line in diff.stdout.splitlines() if line.strip()]
    assert changed_files, (
        "real Claude run produced zero file changes; if this test is "
        "flaky the prompt may need to be tightened"
    )

    # The commit trailer includes the Treadmill IDs (per
    # ``runner._commit_message``).
    log_msg = subprocess.run(
        ["git", "-C", str(verify_dir), "log", "-1", "--format=%B", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert f"Treadmill-Task-Id: {task_id}" in log_msg, log_msg
    assert f"Treadmill-Step-Id: {step_id}" in log_msg, log_msg
