"""Real-Claude multi-step opt-in smoke (Phase 4 C.3 follow-on to B.11).

The Week-3 plan §C.3 calls for an optional real-Claude smoke that
exercises the analyzer→action contract end-to-end: a pre-seeded
analyzer ``task_directive`` is folded into the action role's prompt via
``_compose_prompt``'s ``prior_steps`` handling, the action role
(``role-code-author``) reads the directive, and a file change lands.

Gating
------

Two env vars must be set for the test to run:

  * ``TREADMILL_REAL_CLAUDE=1`` — opt-in switch.
  * ``~/.claude/.credentials.json`` must exist on the host.

CI does not flip ``TREADMILL_REAL_CLAUDE`` by default; this test is
strictly developer-machine smoke.

Cost
----

A single Haiku call with a small directive-shaped prompt is
approximately $0.001 (input + output of a few hundred tokens at the
``claude-haiku-4-5-20251001`` rate). The timeout is capped at 120 s in
the runner so a hung Claude call cannot burn the user's budget.

What this test asserts
----------------------

  1. The runner's ``_execute`` accepts a ``WorkerContext`` with a
     non-empty ``prior_steps`` list and invokes Claude with the
     analyzer's directive folded into the prompt.
  2. Claude makes a file change that's recognizably driven by the
     directive (the directive names a specific file + line content).
  3. The runner commits + pushes the change to the bare repo.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from treadmill_agent import runner
from treadmill_agent.api_client import PriorStep, Role, WorkerContext
from treadmill_agent.config import Settings

from tests.conftest import init_bare_repo


REAL_CLAUDE_GATE = os.environ.get("TREADMILL_REAL_CLAUDE") == "1"
HOST_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"


pytestmark = [
    pytest.mark.skipif(
        not REAL_CLAUDE_GATE,
        reason=(
            "set TREADMILL_REAL_CLAUDE=1 to run the multi-step real-Claude "
            "smoke; this test makes a billable LLM call (~$0.001) per "
            "invocation"
        ),
    ),
    pytest.mark.skipif(
        not HOST_CREDENTIALS.exists(),
        reason=(
            f"{HOST_CREDENTIALS} not found; the real-Claude smoke needs the "
            "user's Claude subscription credentials on the host"
        ),
    ),
    pytest.mark.skipif(
        shutil.which("claude") is None,
        reason="`claude` binary not on PATH; install @anthropic-ai/claude-code",
    ),
]


_CHEAP_MODEL = "claude-haiku-4-5-20251001"


def _multistep_ctx(
    *,
    repo: str,
    task_id: str,
    step_id: str,
    prior_steps: list[PriorStep],
) -> WorkerContext:
    """Build a two-step-shaped WorkerContext for the action role.

    The ``role-code-author``-shaped system prompt nudges Claude to
    obey the directive in ``prior_steps[-1].output.payload
    .task_directive`` exactly; the description is intentionally light
    so the directive is the operative input.
    """
    return WorkerContext(
        step_id=step_id,
        run_id=str(uuid.uuid4()),
        step_index=1,            # action step is the second
        step_name="action",
        status="pending",
        task_id=task_id,
        plan_id=str(uuid.uuid4()),
        repo=repo,
        title="Address review feedback",
        description=(
            "An earlier analyzer step produced a task_directive that "
            "names exactly which file to edit and what to write. "
            "Follow it precisely. Make no other changes."
        ),
        plan_intent=(
            "Exercise the multi-step analyzer→action handoff per "
            "ADR-0015."
        ),
        plan_doc_path=None,
        workflow_id="wf-feedback",
        workflow_version=1,
        trigger="github.pr_review_submitted",
        role=Role(
            id="role-code-author",
            model=_CHEAP_MODEL,
            system_prompt=(
                "You are the code author. The prior step's "
                "task_directive names the file to edit and the exact "
                "line to add. Edit that file, add that line. Touch no "
                "other file. Be terse — one-line summary."
            ),
            skills=[],
            hooks=[],
        ),
        prior_steps=prior_steps,
    )


def _settings(bare_repos_dir: Path, workspace_dir: Path) -> Settings:
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


def test_real_claude_action_role_consumes_prior_step_directive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end multi-step: the action role reads the analyzer's
    ``task_directive`` from ``prior_steps[-1]`` and lands the change
    it describes.

    The directive names ``NOTES.md`` as the target file and "follow
    directive ok" as the line to add. If Claude obeys the directive,
    the post-run diff has a file at that path with that content.
    """
    monkeypatch.delenv("TREADMILL_AGENT_DRY_RUN", raising=False)

    bare_repos_dir = tmp_path / "bare"
    bare_repos_dir.mkdir()
    workspace_dir = tmp_path / "ws"
    workspace_dir.mkdir()

    repo = "treadmill/multistep-real-claude-smoke"
    bare = init_bare_repo(bare_repos_dir, repo)

    task_id = uuid.uuid4().hex
    step_id = uuid.uuid4().hex

    # Pre-seed the analyzer's output as a PriorStep. The directive
    # names the file + content so the test can assert the action role
    # followed it.
    target_file = "NOTES.md"
    target_line = "follow directive ok"
    analyzer = PriorStep(
        step_index=0,
        step_name="analyzer",
        role_id="role-feedback-analyzer",
        status="completed",
        output={
            "summary": "Reviewer asked us to add a single line to NOTES.md.",
            "decision": "plan-ready",
            "payload": {
                "task_directive": {
                    "summary": "Append a marker line to NOTES.md.",
                    "files": [target_file],
                    "intent": (
                        f"Create the file {target_file} (or append if it "
                        f"already exists) containing exactly one line: "
                        f"`{target_line}`. Do not modify any other file."
                    ),
                },
            },
        },
    )

    ctx = _multistep_ctx(
        repo=repo, task_id=task_id, step_id=step_id,
        prior_steps=[analyzer],
    )
    settings = _settings(bare_repos_dir, workspace_dir)

    output = runner._execute(ctx, settings)

    # Branch landed, commit landed.
    assert output.commit_sha, output
    assert len(output.commit_sha) == 40, output.commit_sha

    # Inspect the bare repo's branch tip.
    branch = next(
        (a.value for a in output.artifacts if a.kind == "branch"), None,
    )
    assert branch is not None, output.artifacts
    verify_dir = tmp_path / "verify"
    subprocess.run(
        ["git", "clone", "--branch", branch, str(bare), str(verify_dir)],
        check=True, capture_output=True,
    )
    target = verify_dir / target_file
    assert target.exists(), (
        f"action role did not create {target_file!r}; the directive's "
        "instruction was not followed. Tighten the prompt or pin a more "
        "deterministic model."
    )
    contents = target.read_text()
    assert target_line in contents, (
        f"action role created {target_file!r} but its contents lack "
        f"the directive's required line {target_line!r}. Got: {contents!r}"
    )
