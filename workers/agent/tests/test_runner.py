"""Runner-loop tests.

The runner has one job: read claims, route them through the per-module
primitives, and publish the right lifecycle events. We stub out the
primitives so these tests stay fast and assert the *orchestration* —
order of calls, handling of failures, exit conditions.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from treadmill_agent import claude_code, git, runner
from treadmill_agent.api_client import Role, WorkerContext
from treadmill_agent.config import Settings


def _ctx(**overrides: Any) -> WorkerContext:
    base = WorkerContext(
        step_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        step_index=0,
        step_name="author",
        status="pending",
        task_id=str(uuid.uuid4()),
        plan_id=str(uuid.uuid4()),
        repo="t/repo",
        title="Add a thing",
        description=None,
        plan_intent="goal",
        plan_doc_path=None,
        workflow_id="wf-author",
        workflow_version=1,
        trigger="registered",
        role=Role(
            id="role-author", model="claude-opus-4-7",
            system_prompt="be a coder",
            output_kind="code",
            skills=[], hooks=[],
        ),
        pr_number=None,
        prior_steps=[],
    )
    return replace(base, **overrides) if overrides else base


def _settings(**overrides: Any) -> Settings:
    base = dict(
        api_url="http://fake",
        work_queue_url="http://sqs/q",
        events_topic_arn="arn",
        aws_endpoint_url=None,
        aws_region="us-east-1",
        repo_mode="local",
        bare_repos_dir="/tmp/bare",
        workspace_dir="/tmp/ws",
        exit_after_step=True,
        poll_wait_seconds=1,
        claude_credentials_path="/root/.claude/.credentials.json",
    )
    base.update(overrides)
    return Settings(**base)


class _FakeSqs:
    def __init__(self, claims: list[dict]) -> None:
        # Each call to receive_message pops the next claim batch.
        self._claims = list(claims)
        self.deleted: list[str] = []
        # ADR-0025: record visibility-extension calls so heartbeat tests
        # can assert the heartbeat thread fires while a worker is in
        # flight.
        self.visibility_changes: list[dict] = []

    def receive_message(self, **kwargs) -> dict:
        if not self._claims:
            return {"Messages": []}
        return {"Messages": [self._claims.pop(0)]}

    def delete_message(self, *, QueueUrl: str, ReceiptHandle: str) -> None:
        self.deleted.append(ReceiptHandle)

    def change_message_visibility(
        self, *, QueueUrl: str, ReceiptHandle: str, VisibilityTimeout: int,
    ) -> None:
        self.visibility_changes.append({
            "QueueUrl": QueueUrl,
            "ReceiptHandle": ReceiptHandle,
            "VisibilityTimeout": VisibilityTimeout,
        })


class _FakeApi:
    def __init__(self, ctx: WorkerContext) -> None:
        self._ctx = ctx
        self.fetched: list[str] = []

    def fetch_step_context(self, step_id: str) -> WorkerContext:
        self.fetched.append(step_id)
        return self._ctx


class _FakePublisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def publish_step_started(self, **kwargs) -> None:
        self.events.append(("started", kwargs))

    def publish_step_completed(self, **kwargs) -> None:
        self.events.append(("completed", kwargs))

    def publish_step_failed(self, **kwargs) -> None:
        self.events.append(("failed", kwargs))


def _claim(
    step_id: str,
    *,
    task_id: str | None = None,
    plan_id: str | None = None,
    run_id: str | None = None,
    receipt: str = "rh",
) -> dict:
    """Build a fake SQS claim body. Defaults task/plan/run IDs to fresh
    UUIDs so tests that don't care about them stay readable.

    The dispatcher (API-side) puts all four IDs in the JSON body — the
    worker needs them up-front so ``step.started`` can be published
    before the API round-trip in ``fetch_step_context``.
    """
    return {
        "Body": json.dumps({
            "step_id": step_id,
            "task_id": task_id or str(uuid.uuid4()),
            "plan_id": plan_id or str(uuid.uuid4()),
            "run_id": run_id or str(uuid.uuid4()),
        }),
        "ReceiptHandle": receipt,
    }


# ── happy path ────────────────────────────────────────────────────────────────


def test_runner_publishes_started_then_completed_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner forwards ``_execute``'s ``StepOutput`` envelope verbatim
    to ``publish_step_completed``. Per ADR-0012 the envelope is what the
    consumer reads."""
    from treadmill_agent.events import Artifact, StepOutput

    ctx = _ctx()
    envelope = StepOutput(
        summary="did it",
        decision="pushed",
        commit_sha="abc",
        artifacts=[
            Artifact(kind="branch", value="task/x"),
            Artifact(kind="pr_url", value="https://x"),
        ],
        payload={"pr_number": 7},
    )
    monkeypatch.setattr(runner, "_execute", lambda c, s: envelope)
    sqs = _FakeSqs([_claim(ctx.step_id, receipt="rh-1")])
    api = _FakeApi(ctx)
    pub = _FakePublisher()

    n = runner.run(settings=_settings(), api=api, sqs_client=sqs, publisher=pub)
    assert n == 1
    assert api.fetched == [ctx.step_id]
    assert [name for name, _ in pub.events] == ["started", "completed"]
    completed_kwargs = pub.events[1][1]
    out = completed_kwargs["output"]
    assert isinstance(out, StepOutput)
    assert out.payload["pr_number"] == 7
    assert out.commit_sha == "abc"
    branches = [a.value for a in out.artifacts if a.kind == "branch"]
    assert branches == ["task/x"]
    assert sqs.deleted == ["rh-1"]


def test_runner_publishes_started_then_failed_when_fetch_context_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``api.fetch_step_context`` raises (e.g. the API is down), the
    worker still publishes ``step.started`` first — the dispatcher put
    all four IDs in the claim body so the worker doesn't need the API
    response to identify the step. ``step.failed`` follows with the
    fetch error.

    This is the load-bearing observability win from B.4: a step that
    enters execution always emits ``started`` before any failure, so
    the consumer's audit trail never has a ``failed`` floating without
    a corresponding ``started``.

    Per ADR-0025 (don't-delete-on-error), the underlying API failure
    propagates out of ``_handle_step`` and ``run`` — the SQS message is
    NOT deleted so visibility expiry redelivers / DLQs it.
    """
    class _BoomApi:
        def fetch_step_context(self, step_id: str) -> WorkerContext:
            raise RuntimeError("api down")

    step_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    plan_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    sqs = _FakeSqs([_claim(
        step_id, task_id=task_id, plan_id=plan_id, run_id=run_id,
        receipt="rh-1",
    )])
    pub = _FakePublisher()
    with pytest.raises(RuntimeError, match="api down"):
        runner.run(
            settings=_settings(), api=_BoomApi(),
            sqs_client=sqs, publisher=pub,
        )

    names = [name for name, _ in pub.events]
    assert names == ["started", "failed"]
    # Started carries the IDs from the claim body — the worker had them
    # without ever talking to the API.
    started_kwargs = pub.events[0][1]
    assert started_kwargs["step_id"] == step_id
    assert started_kwargs["task_id"] == task_id
    assert started_kwargs["plan_id"] == plan_id
    assert started_kwargs["run_id"] == run_id
    # Failed carries the same IDs and the error message.
    failed_kwargs = pub.events[1][1]
    assert failed_kwargs["step_id"] == step_id
    assert "api down" in failed_kwargs["error"]
    # ADR-0025: don't-delete-on-error — message stays in flight for SQS
    # to redeliver / DLQ.
    assert sqs.deleted == []


def test_runner_publishes_failed_when_execute_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per ADR-0025: ``_execute`` raising publishes ``step.failed`` for the
    audit trail and then propagates the exception out of ``run`` — the
    SQS message is NOT deleted so SQS redelivers (and eventually DLQs)
    after ``maxReceiveCount`` retries.
    """
    ctx = _ctx()

    def _boom(*args, **kwargs):
        raise RuntimeError("git push rejected")
    monkeypatch.setattr(runner, "_execute", _boom)

    sqs = _FakeSqs([_claim(ctx.step_id, receipt="rh-1")])
    pub = _FakePublisher()
    with pytest.raises(RuntimeError, match="git push rejected"):
        runner.run(
            settings=_settings(), api=_FakeApi(ctx),
            sqs_client=sqs, publisher=pub,
        )

    names = [name for name, _ in pub.events]
    assert names == ["started", "failed"]
    assert pub.events[1][1]["error"] == "git push rejected"
    # ADR-0025: don't-delete-on-error — visibility expiry handles retry.
    assert sqs.deleted == []


# ── empty queue ───────────────────────────────────────────────────────────────


def test_runner_exits_clean_when_queue_empty() -> None:
    sqs = _FakeSqs([])  # no messages
    n = runner.run(
        settings=_settings(), api=_FakeApi(_ctx()),
        sqs_client=sqs, publisher=_FakePublisher(),
    )
    assert n == 0


# ── exit-after-step ───────────────────────────────────────────────────────────


def test_runner_exits_after_one_step_when_flag_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``EXIT_AFTER_STEP=true`` (the autoscaler-friendly default) makes
    the runner process exactly one claim and return — even if more are
    pending. The autoscaler launches a fresh replica per message."""
    from treadmill_agent.events import Artifact, StepOutput

    def _fake_execute(c, s):
        return StepOutput(
            summary="ok", decision="pushed", commit_sha="abc",
            artifacts=[Artifact(kind="branch", value="task/x")],
        ), None
    monkeypatch.setattr(runner, "_execute", _fake_execute)
    ctx = _ctx()
    sqs = _FakeSqs([
        _claim(ctx.step_id, receipt="rh-1"),
        _claim(ctx.step_id, receipt="rh-2"),
        _claim(ctx.step_id, receipt="rh-3"),
    ])
    pub = _FakePublisher()
    n = runner.run(
        settings=_settings(exit_after_step=True), api=_FakeApi(ctx),
        sqs_client=sqs, publisher=pub,
    )
    assert n == 1
    assert sqs.deleted == ["rh-1"]
    # 1 started + 1 completed = 2 events.
    assert len(pub.events) == 2


def test_runner_continues_polling_when_exit_after_step_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the flag off, the runner drains the queue until empty.

    The dev-mode use case: keep one worker attached and watch it process
    everything in one shell session.
    """
    from treadmill_agent.events import Artifact, StepOutput

    def _fake_execute(c, s):
        return StepOutput(
            summary="ok", decision="pushed", commit_sha="abc",
            artifacts=[Artifact(kind="branch", value="task/x")],
        ), None
    monkeypatch.setattr(runner, "_execute", _fake_execute)
    ctx = _ctx()
    sqs = _FakeSqs([
        _claim(ctx.step_id, receipt="rh-1"),
        _claim(ctx.step_id, receipt="rh-2"),
        _claim(ctx.step_id, receipt="rh-3"),
    ])
    pub = _FakePublisher()
    n = runner.run(
        settings=_settings(exit_after_step=False), api=_FakeApi(ctx),
        sqs_client=sqs, publisher=pub,
    )
    # All three claims drained; empty receive returned the runner.
    assert n == 3
    assert sqs.deleted == ["rh-1", "rh-2", "rh-3"]
    # 3 started + 3 completed = 6 events.
    assert len(pub.events) == 6


# ── malformed claim ───────────────────────────────────────────────────────────


def test_runner_drops_malformed_claim_without_publishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claim missing step_id is logged + deleted; nothing is published."""
    sqs = _FakeSqs([{
        "Body": "{not valid json}",
        "ReceiptHandle": "rh-bad",
    }])
    pub = _FakePublisher()
    n = runner.run(
        settings=_settings(), api=_FakeApi(_ctx()),
        sqs_client=sqs, publisher=pub,
    )
    # The malformed message was eaten; the runner returned 0 because
    # _receive_one returned None and the next poll was empty.
    assert n == 0
    assert pub.events == []
    assert sqs.deleted == ["rh-bad"]


# ── branch + commit message ───────────────────────────────────────────────────


def test_branch_for_step_matches_adr_0010_format() -> None:
    """ADR-0010 §"Branch conventions": ``task/<short-id>-<slugified-title>``.

    The short ID is the first 8 hex chars of the task UUID with hyphens
    stripped; the slug comes from the task title. Step name is NOT in
    the branch — v0 is single-step.
    """
    ctx = _ctx(
        task_id="abcdef12-3456-7890-abcd-ef1234567890",
        title="Add a thing",
        step_name="author",
    )
    assert runner._branch_for_step(ctx) == "task/abcdef12-add-a-thing"


def test_slugify_strips_non_ascii_and_punctuation() -> None:
    # Non-ASCII letters collapse into ``-``; punctuation likewise. Runs
    # of separators collapse to a single ``-``.
    assert runner._slugify_title("Héllo, World!") == "h-llo-world"
    assert runner._slugify_title("foo___bar...baz") == "foo-bar-baz"
    assert runner._slugify_title("ünicode 漢字 emoji 🚀") == "nicode-emoji"


def test_slugify_handles_empty_and_punctuation_only_titles() -> None:
    assert runner._slugify_title("") == "untitled"
    assert runner._slugify_title("   ") == "untitled"
    assert runner._slugify_title("...!!!---") == "untitled"
    assert runner._slugify_title("漢字") == "untitled"


def test_slugify_truncates_long_titles() -> None:
    long_title = "this is a really long task title that goes on and on forever"
    slug = runner._slugify_title(long_title)
    assert len(slug) <= 40
    # Cut at a word boundary — last char is alphanumeric, no trailing dash.
    assert not slug.endswith("-")
    # Single hard cut without a word break: produce something <=40 chars.
    no_breaks = "a" * 60
    assert len(runner._slugify_title(no_breaks)) <= 40


def test_slugify_idempotent() -> None:
    """Re-slugifying an already-slugified value returns the same value."""
    for title in [
        "Add a thing",
        "Héllo, World!",
        "foo___bar...baz",
        "this is a really long task title that goes on and on forever",
        "",
        "...!!!---",
        "trailing-and-leading---",
    ]:
        once = runner._slugify_title(title)
        twice = runner._slugify_title(once)
        assert once == twice, f"slugify not idempotent for {title!r}: {once!r} -> {twice!r}"


@pytest.mark.parametrize("title", [
    "../../../etc/passwd",
    "../..\\..\\windows",
    "name with spaces",
    "rm -rf / ; echo pwned",
    "back`tick`s and $vars",
    "pipes | and & ampersands",
    "newline\nin\ttitle",
    "null\x00byte",
    "a/b/c/path-like",
    "$(whoami) injection",
])
def test_slugify_never_contains_path_traversal_or_shell_meta(title: str) -> None:
    """Adversarial input — slug output must be restricted to ``[a-z0-9-]``
    so it can't escape a branch name into the filesystem or a shell."""
    slug = runner._slugify_title(title)
    for forbidden in ["..", "/", " ", ";", "&", "|", "$", "`", "\n", "\0", "\\", "\t"]:
        assert forbidden not in slug, (
            f"forbidden char {forbidden!r} in slug {slug!r} for input {title!r}"
        )
    # Belt-and-suspenders: the slug matches the documented charset.
    import re
    assert re.fullmatch(r"[a-z0-9-]+", slug), f"unexpected chars in {slug!r}"


def _init_bare(bare_repos_dir: Path, repo: str) -> Path:
    """Mirror ``tests/test_git.py``'s bare-repo seeding — a real git
    fixture so ``_execute`` exercises the actual git primitives."""
    bare = bare_repos_dir / f"{git.repo_to_directory_name(repo)}.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(bare)],
        check=True,
    )
    seed = bare_repos_dir.parent / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main", str(seed)], check=True)
    (seed / "README.md").write_text("# repo\n")
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(bare)], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], check=True)
    shutil.rmtree(seed)
    return bare


def test_execute_publishes_failed_when_no_changes_authored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Real Claude that produces zero file edits must surface as
    ``step.failed``. ``_execute`` stages the working tree, checks for
    staged changes, and raises ``CodeAuthorError`` when none exist —
    the runner catches it and publishes ``failed`` with "no changes" in
    the error message.

    This is the load-bearing B.2 invariant: the smoke test can no longer
    claim "Phase 2 success criterion satisfied" via an empty commit.
    """
    bare_repos_dir = tmp_path / "bare"
    bare_repos_dir.mkdir()
    workspace_dir = tmp_path / "ws"
    workspace_dir.mkdir()
    _init_bare(bare_repos_dir, "owner/test-repo")

    # Make claude_code a no-op — it returns a summary but writes
    # nothing to the working tree. That's the exact production failure
    # mode B.2 must catch.
    monkeypatch.setattr(
        claude_code, "run_claude_code",
        lambda **_: claude_code.CodeAuthorResult(summary="did nothing"),
    )
    # Real Claude path: dry-run off.
    monkeypatch.delenv("TREADMILL_AGENT_DRY_RUN", raising=False)

    ctx = _ctx(repo="owner/test-repo")
    sqs = _FakeSqs([_claim(
        ctx.step_id, task_id=ctx.task_id, plan_id=ctx.plan_id,
        run_id=ctx.run_id, receipt="rh-1",
    )])
    pub = _FakePublisher()
    with pytest.raises(Exception, match="(?i)no changes"):
        runner.run(
            settings=_settings(
                bare_repos_dir=str(bare_repos_dir),
                workspace_dir=str(workspace_dir),
            ),
            api=_FakeApi(ctx),
            sqs_client=sqs,
            publisher=pub,
        )

    names = [name for name, _ in pub.events]
    assert names == ["started", "failed"]
    error = pub.events[1][1]["error"]
    assert "no changes" in error.lower()
    # ADR-0025: don't-delete-on-error — the message stays in flight so
    # SQS can redeliver / DLQ. The audit trail in events still tells
    # the operator what went wrong.
    assert sqs.deleted == []


def test_commit_message_includes_task_and_step_trailers() -> None:
    ctx = _ctx()
    msg = runner._commit_message(ctx)
    assert ctx.title in msg
    assert f"Treadmill-Task-Id: {ctx.task_id}" in msg
    assert f"Treadmill-Step-Id: {ctx.step_id}" in msg


# ── _execute returns StepOutput envelope (ADR-0012) ──────────────────────────


def _setup_execute_fixture(
    tmp_path: Path,
    *,
    repo: str = "owner/test-repo",
) -> tuple[Path, Path]:
    """Common bare-repo + workspace setup for ``_execute`` integration tests."""
    bare_repos_dir = tmp_path / "bare"
    bare_repos_dir.mkdir()
    workspace_dir = tmp_path / "ws"
    workspace_dir.mkdir()
    _init_bare(bare_repos_dir, repo)
    return bare_repos_dir, workspace_dir


def test_execute_returns_step_output_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Per ADR-0012, ``_execute`` returns a uniform ``StepOutput``
    envelope (not a dict). The envelope's required fields land at
    the documented top-level positions."""
    from treadmill_agent.events import StepOutput

    bare_repos_dir, workspace_dir = _setup_execute_fixture(tmp_path)
    # Dry-run path: ``_dry_run_author`` writes a marker file so the
    # commit + push succeed without invoking real Claude.
    monkeypatch.setenv("TREADMILL_AGENT_DRY_RUN", "1")
    ctx = _ctx(repo="owner/test-repo")
    output, token_usage = runner._execute(
        ctx,
        _settings(
            bare_repos_dir=str(bare_repos_dir),
            workspace_dir=str(workspace_dir),
        ),
    )

    assert isinstance(output, StepOutput)
    # Dry-run skips the LLM ⇒ no token_usage telemetry.
    assert token_usage is None
    # ``summary`` is required + populated by the dry-run authoring marker.
    assert output.summary  # truthy
    # ``decision`` is required; per the prompt-spec ``wf-author`` emits
    # ``pushed`` on the successful path.
    assert output.decision == "pushed"
    # ``metadata`` left empty for v0.
    from treadmill_agent.events import Metadata
    assert output.metadata == Metadata()


def test_execute_includes_branch_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Per ADR-0012's wf-author convention map, ``branch`` lives as an
    ``Artifact(kind="branch", ...)`` in the envelope's artifact list."""
    bare_repos_dir, workspace_dir = _setup_execute_fixture(tmp_path)
    monkeypatch.setenv("TREADMILL_AGENT_DRY_RUN", "1")
    ctx = _ctx(
        repo="owner/test-repo",
        task_id="abcdef12-3456-7890-abcd-ef1234567890",
        title="Add a thing",
    )
    output, _ = runner._execute(
        ctx,
        _settings(
            bare_repos_dir=str(bare_repos_dir),
            workspace_dir=str(workspace_dir),
        ),
    )

    branches = [a for a in output.artifacts if a.kind == "branch"]
    assert len(branches) == 1
    # Branch follows ADR-0010's convention: task/<short-id>-<slug>.
    assert branches[0].value == "task/abcdef12-add-a-thing"


def test_execute_includes_commit_sha_top_level(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Per ADR-0012, ``commit_sha`` is top-level (not in ``payload``)
    because ADR-0013's mergeability VIEW joins on
    ``output->>'commit_sha'``. Confirm the field is populated with a
    real git SHA after a successful commit + push."""
    bare_repos_dir, workspace_dir = _setup_execute_fixture(tmp_path)
    monkeypatch.setenv("TREADMILL_AGENT_DRY_RUN", "1")
    ctx = _ctx(repo="owner/test-repo")
    output, _ = runner._execute(
        ctx,
        _settings(
            bare_repos_dir=str(bare_repos_dir),
            workspace_dir=str(workspace_dir),
        ),
    )

    # Top-level — not in payload.
    assert output.commit_sha is not None
    assert "commit_sha" not in output.payload
    # A real git SHA is 40 lowercase hex chars.
    import re
    assert re.fullmatch(r"[0-9a-f]{40}", output.commit_sha), (
        f"commit_sha does not look like a git SHA: {output.commit_sha!r}"
    )


def test_execute_local_mode_omits_pr_number_from_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``repo_mode='local'`` (default in tests) does not open a PR — the
    worker's ``git.open_pr`` returns ``(None, None)``. Per the spec,
    ``pr_number=None`` is encoded by *omitting* the key from
    ``payload`` (not by writing ``None``)."""
    bare_repos_dir, workspace_dir = _setup_execute_fixture(tmp_path)
    monkeypatch.setenv("TREADMILL_AGENT_DRY_RUN", "1")
    ctx = _ctx(repo="owner/test-repo")
    output, _ = runner._execute(
        ctx,
        _settings(
            bare_repos_dir=str(bare_repos_dir),
            workspace_dir=str(workspace_dir),
        ),
    )

    # Local-mode never opens a PR — no pr_url artifact + no pr_number key.
    pr_urls = [a for a in output.artifacts if a.kind == "pr_url"]
    assert pr_urls == []
    assert "pr_number" not in output.payload


# ── Dry-run analyzer extension (D.1 hand-off) ────────────────────────────────


def test_is_analyzer_role_matches_all_starter_analyzers() -> None:
    """``_is_analyzer_role`` must match every analyzer-role id in
    ``starters.py`` and reject every action-role id. If a future role
    addition breaks this invariant, the dry-run analyzer extension
    silently degrades — surface it here so the build fails first."""
    assert runner._is_analyzer_role("role-feedback-analyzer")
    assert runner._is_analyzer_role("role-ci-analyzer")
    assert runner._is_analyzer_role("role-conflict-analyzer")
    assert runner._is_analyzer_role("role-planner")
    # Action / single-step roles are NOT analyzers.
    assert not runner._is_analyzer_role("role-code-author")
    assert not runner._is_analyzer_role("role-doc-author")
    assert not runner._is_analyzer_role("role-reviewer")
    assert not runner._is_analyzer_role("role-validator")


def test_execute_dry_run_analyzer_emits_task_directive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Per Week-3 plan §D.1's hand-off: when the dry-run path runs an
    analyzer step (role id ending in ``-analyzer`` or ``role-planner``),
    the StepOutput's ``payload.task_directive`` is non-empty so the
    downstream action step's ``prior_steps[-1].output.payload
    .task_directive`` is something the action role can read.

    Asserts the directive shape mirrors ADR-0015's ``TaskDirective``
    convention + ADR-0010's ``TaskSpec`` (intent / files / summary)."""
    bare_repos_dir, workspace_dir = _setup_execute_fixture(tmp_path)
    monkeypatch.setenv("TREADMILL_AGENT_DRY_RUN", "1")
    ctx = _ctx(
        repo="owner/test-repo",
        workflow_id="wf-ci-fix",
        step_index=0,
        step_name="analyzer",
        role=Role(
            id="role-ci-analyzer", model="claude-haiku-4-5-20251001",
            system_prompt="be an analyzer",
            output_kind="analysis",
            skills=[], hooks=[],
        ),
    )
    output, _ = runner._execute(
        ctx,
        _settings(
            bare_repos_dir=str(bare_repos_dir),
            workspace_dir=str(workspace_dir),
        ),
    )

    assert "task_directive" in output.payload, output.payload
    directive = output.payload["task_directive"]
    # Shape contract per ADR-0015 §"task_directive".
    assert isinstance(directive.get("summary"), str) and directive["summary"]
    assert isinstance(directive.get("intent"), str) and directive["intent"]
    assert isinstance(directive.get("files"), list) and directive["files"]


# ── ADR-0022: per-kind dispatch ──────────────────────────────────────────────


def test_execute_dispatches_to_handler_for_role_output_kind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The runner reads ``ctx.role.output_kind`` and looks up the handler
    in ``DISPOSITIONS``. Per ADR-0022, this is the load-bearing
    routing — without it, every non-``code`` role would hit the
    diff/commit/push path and fail."""
    from treadmill_agent.events import Artifact, Metadata, StepOutput

    bare_repos_dir, workspace_dir = _setup_execute_fixture(tmp_path)
    monkeypatch.setenv("TREADMILL_AGENT_DRY_RUN", "1")

    calls: list[str] = []

    def _fake_handler(disp_ctx) -> StepOutput:
        calls.append(disp_ctx.ctx.role.output_kind)
        return StepOutput(
            summary="dispatched",
            decision="ok",
            commit_sha=None,
            artifacts=[Artifact(kind="analysis", value="seen")],
            payload={},
            metadata=Metadata(),
        )

    monkeypatch.setitem(runner.DISPOSITIONS, "analysis", _fake_handler)

    ctx = _ctx(
        repo="owner/test-repo",
        role=Role(
            id="role-analyzer-test",
            model="claude-haiku-4-5-20251001",
            system_prompt="be an analyzer",
            output_kind="analysis",
            skills=[], hooks=[],
        ),
    )
    out, _ = runner._execute(
        ctx,
        _settings(
            bare_repos_dir=str(bare_repos_dir),
            workspace_dir=str(workspace_dir),
        ),
    )
    assert calls == ["analysis"]
    assert out.decision == "ok"


def test_execute_dry_run_action_role_omits_task_directive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The action role's dry-run path leaves ``payload.task_directive``
    absent — directives are produced by analyzers, not consumed by them.
    Asserts the action role's payload is unchanged from the single-step
    shape (no spurious ``task_directive`` key)."""
    bare_repos_dir, workspace_dir = _setup_execute_fixture(tmp_path)
    monkeypatch.setenv("TREADMILL_AGENT_DRY_RUN", "1")
    ctx = _ctx(
        repo="owner/test-repo",
        workflow_id="wf-ci-fix",
        step_index=1,
        step_name="action",
        role=Role(
            id="role-code-author", model="claude-haiku-4-5-20251001",
            system_prompt="be a coder",
            output_kind="code",
            skills=[], hooks=[],
        ),
    )
    output, _ = runner._execute(
        ctx,
        _settings(
            bare_repos_dir=str(bare_repos_dir),
            workspace_dir=str(workspace_dir),
        ),
    )

    assert "task_directive" not in output.payload, output.payload


# ── ADR-0025: heartbeat thread + don't-delete-on-error ──────────────────────


def test_runner_heartbeat_extends_visibility_during_long_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0025: while ``_handle_step`` runs, a daemon heartbeat thread
    calls ``change_message_visibility`` every
    ``HEARTBEAT_INTERVAL_SECONDS`` with
    ``VisibilityTimeout=VISIBILITY_EXTENSION_SECONDS=120``.

    We accelerate the heartbeat cadence by monkey-patching
    ``HEARTBEAT_INTERVAL_SECONDS`` to a tiny value and have ``_execute``
    block until at least two visibility extensions have been recorded —
    that simulates a 60-second work block without actually sleeping for
    60s.
    """
    import threading as _threading
    from treadmill_agent.events import Artifact, StepOutput

    # Tiny heartbeat interval so the test runs in milliseconds.
    monkeypatch.setattr(runner, "HEARTBEAT_INTERVAL_SECONDS", 0.01)

    ctx = _ctx()
    fires_recorded = _threading.Event()
    minimum_fires = 2

    # Wrap _FakeSqs.change_message_visibility so we can signal when
    # enough heartbeats have fired.
    sqs = _FakeSqs([_claim(ctx.step_id, receipt="rh-hb")])
    real_change = sqs.change_message_visibility

    def _counting_change(**kwargs):
        real_change(**kwargs)
        if len(sqs.visibility_changes) >= minimum_fires:
            fires_recorded.set()

    sqs.change_message_visibility = _counting_change  # type: ignore[method-assign]

    def _slow_execute(c, s):
        # Block until the heartbeat has fired at least minimum_fires
        # times, then return successfully. ``timeout`` is a backstop so
        # a regression (heartbeat never fires) fails fast rather than
        # hanging.
        if not fires_recorded.wait(timeout=5.0):
            raise AssertionError(
                "heartbeat did not fire while _execute was running; "
                f"visibility_changes={sqs.visibility_changes!r}"
            )
        return StepOutput(
            summary="ok", decision="pushed", commit_sha="abc",
            artifacts=[Artifact(kind="branch", value="task/x")],
        ), None

    monkeypatch.setattr(runner, "_execute", _slow_execute)

    pub = _FakePublisher()
    runner.run(
        settings=_settings(), api=_FakeApi(ctx),
        sqs_client=sqs, publisher=pub,
    )

    # At least minimum_fires visibility extensions were recorded; each
    # used VISIBILITY_EXTENSION_SECONDS=120 against the right handle.
    assert len(sqs.visibility_changes) >= minimum_fires, sqs.visibility_changes
    for call in sqs.visibility_changes:
        assert call["VisibilityTimeout"] == runner.VISIBILITY_EXTENSION_SECONDS
        assert call["VisibilityTimeout"] == 120
        assert call["ReceiptHandle"] == "rh-hb"
    # Happy path → message deleted as usual.
    assert sqs.deleted == ["rh-hb"]


def test_runner_does_not_delete_when_disposition_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0025 don't-delete-on-error: when the dispatch handler raises,
    the worker publishes ``step.failed`` for the audit trail and lets
    the exception propagate; ``delete_message`` is NEVER called so SQS
    redelivers via visibility expiry.
    """
    ctx = _ctx()

    def _boom(*args, **kwargs):
        raise RuntimeError("disposition exploded")
    monkeypatch.setattr(runner, "_execute", _boom)

    sqs = _FakeSqs([_claim(ctx.step_id, receipt="rh-dispo")])
    pub = _FakePublisher()
    with pytest.raises(RuntimeError, match="disposition exploded"):
        runner.run(
            settings=_settings(), api=_FakeApi(ctx),
            sqs_client=sqs, publisher=pub,
        )

    # The defining assertion for ADR-0025's don't-delete-on-error:
    # ``delete_message`` was NEVER called even though the worker
    # entered, started, and failed the step.
    assert sqs.deleted == []
    names = [name for name, _ in pub.events]
    assert names == ["started", "failed"]


def test_runner_stops_heartbeat_in_finally_even_on_disposition_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0025: the ``finally`` block must call ``stop_event.set()`` and
    join the heartbeat thread even when the disposition raises — otherwise
    a stuck heartbeat thread would keep extending the visibility on an
    orphaned message after the worker process exits (and would block
    process exit in the non-daemon case).

    Spy on ``threading.Thread`` construction so we capture the
    ``stop_event`` passed to ``_run_heartbeat`` and assert it is set
    after ``runner.run`` raises.
    """
    import threading as _threading
    ctx = _ctx()

    captured: dict[str, Any] = {}
    real_thread_cls = _threading.Thread

    class _SpyThread(real_thread_cls):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            # The heartbeat thread is the one whose target is
            # ``_run_heartbeat``; capture its stop_event for the assert.
            if kwargs.get("target") is runner._run_heartbeat:
                # args = (sqs_client, queue_url, receipt_handle, stop_event)
                captured["stop_event"] = kwargs["args"][3]
                captured["thread"] = self

    monkeypatch.setattr(_threading, "Thread", _SpyThread)
    # The runner imports ``threading`` at module level, so patch the
    # module-level reference too.
    monkeypatch.setattr(runner.threading, "Thread", _SpyThread)

    def _boom(*args, **kwargs):
        raise RuntimeError("kaboom in disposition")
    monkeypatch.setattr(runner, "_execute", _boom)

    sqs = _FakeSqs([_claim(ctx.step_id, receipt="rh-fin")])
    pub = _FakePublisher()
    with pytest.raises(RuntimeError, match="kaboom in disposition"):
        runner.run(
            settings=_settings(), api=_FakeApi(ctx),
            sqs_client=sqs, publisher=pub,
        )

    # The ``finally`` block in ``run`` must have fired and set the stop
    # event — otherwise the heartbeat thread would still be running.
    assert "stop_event" in captured, "heartbeat thread was never spawned"
    assert captured["stop_event"].is_set(), (
        "stop_event was not set in finally — heartbeat thread leaks"
    )
    # And the daemon heartbeat thread has cleanly exited (join returned).
    captured["thread"].join(timeout=2.0)
    assert not captured["thread"].is_alive(), (
        "heartbeat thread did not exit after stop_event.set()"
    )


# ── Queue-hygiene contract (ADR-0048 verification) ───────────────────────────
#
# The contract:
#
#   A worker must NOT ack/delete an SQS message it didn't successfully
#   process. When the worker raises an uncaught exception mid-task — for
#   any reason — the message stays in flight, SQS expires the visibility
#   lease (~60s), and the message is redelivered. After maxReceiveCount=5
#   it lands in the DLQ for operator inspection.
#
# This is the recovery mechanism for the ``validate-crash-no-retry`` and
# ``review-crash-no-retry`` dead-end classes that appear in the
# ``docs/diagrams/task-flow-dead-ends.md`` catalog: they aren't actually
# terminal — queue redelivery handles them — but only if this contract
# holds. The tests below cover every distinct exception class the spec
# called out:
#
#   * subprocess crash (CodeAuthorError raised by the claude_code module
#     when the LLM subprocess exits non-zero)
#   * Python uncaught exception in the work path
#   * network failure mid-work (e.g. boto3 ClientError reaching GitHub)
#   * subprocess SIGKILL-style non-zero exit propagated through
#     subprocess.CalledProcessError
#
# Each test asserts ``delete_message`` was NEVER called even though the
# worker entered and started the step. Together with the existing
# ``test_runner_does_not_delete_when_*`` tests above, this nails the
# contract down at the dispatch boundary.


def test_queue_hygiene_no_ack_on_subprocess_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subprocess crash: the Claude Code child process exits non-zero
    mid-task and ``claude_code.run_claude_code`` raises ``CodeAuthorError``.

    The runner must catch it for ``step.failed`` publication, then re-raise
    without ever calling ``delete_message``. SQS visibility expiry handles
    the redelivery.
    """
    from treadmill_agent.claude_code import CodeAuthorError

    ctx = _ctx()

    def _subprocess_died(*args, **kwargs):
        # Mirrors what ``claude_code._run_claude_code`` raises when the
        # subprocess exits with returncode != 0 — see claude_code.py:243.
        raise CodeAuthorError("claude exited 137\n<stderr: OOM-killed>")
    monkeypatch.setattr(runner, "_execute", _subprocess_died)

    sqs = _FakeSqs([_claim(ctx.step_id, receipt="rh-subproc")])
    pub = _FakePublisher()
    with pytest.raises(CodeAuthorError, match="claude exited 137"):
        runner.run(
            settings=_settings(), api=_FakeApi(ctx),
            sqs_client=sqs, publisher=pub,
        )

    # The defining contract assertion: NO delete on subprocess crash.
    assert sqs.deleted == [], (
        "queue-hygiene contract broken: worker acked SQS message after "
        "subprocess crash; SQS would never redeliver and the task would "
        "stick at validate-crash-no-retry / review-crash-no-retry"
    )
    # The audit trail still tells the operator the step failed.
    assert [name for name, _ in pub.events] == ["started", "failed"]


def test_queue_hygiene_no_ack_on_network_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Network failure mid-work: e.g. boto3 ``ClientError`` reaching out
    to GitHub or AWS. The work didn't succeed; the message must NOT be
    acked.
    """
    from botocore.exceptions import ClientError

    ctx = _ctx()

    def _network_died(*args, **kwargs):
        raise ClientError(
            error_response={"Error": {"Code": "RequestTimeout", "Message": "timed out"}},
            operation_name="PutObject",
        )
    monkeypatch.setattr(runner, "_execute", _network_died)

    sqs = _FakeSqs([_claim(ctx.step_id, receipt="rh-net")])
    pub = _FakePublisher()
    with pytest.raises(ClientError):
        runner.run(
            settings=_settings(), api=_FakeApi(ctx),
            sqs_client=sqs, publisher=pub,
        )

    assert sqs.deleted == [], (
        "queue-hygiene contract broken: worker acked SQS message after "
        "transient network failure; the work didn't succeed so SQS must "
        "redeliver via visibility expiry"
    )
    assert [name for name, _ in pub.events] == ["started", "failed"]


def test_queue_hygiene_no_ack_on_subprocess_called_process_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``subprocess.CalledProcessError`` — what a non-Claude subprocess
    (e.g. ``git push``, ``gh pr create``) raises when ``check=True`` and
    the exit code is non-zero. The runner must NOT ack on this either.

    This catches an OS-level kill of a child process the worker
    spawned: SIGKILL from OOM-killer manifests as a non-zero returncode
    + ``CalledProcessError`` when ``check=True`` is set on the
    subprocess call (which the worker's git + gh modules do).
    """
    import subprocess as _subprocess

    ctx = _ctx()

    def _called_process_died(*args, **kwargs):
        raise _subprocess.CalledProcessError(
            returncode=-9,  # SIGKILL
            cmd=["git", "push", "origin", "task/x"],
            stderr=b"Killed",
        )
    monkeypatch.setattr(runner, "_execute", _called_process_died)

    sqs = _FakeSqs([_claim(ctx.step_id, receipt="rh-killed")])
    pub = _FakePublisher()
    with pytest.raises(_subprocess.CalledProcessError):
        runner.run(
            settings=_settings(), api=_FakeApi(ctx),
            sqs_client=sqs, publisher=pub,
        )

    assert sqs.deleted == [], (
        "queue-hygiene contract broken: worker acked SQS message after "
        "child subprocess was SIGKILL'd / exited non-zero"
    )
    assert [name for name, _ in pub.events] == ["started", "failed"]


# ── ADR-0049: per-task repo-scoped GitHub App token mint ─────────────────────


def test_handle_step_mints_repo_scoped_token_before_execute_in_app_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0049: when the worker is in ``repo_mode='github'`` +
    ``github_auth_mode='app'``, ``_handle_step`` must call
    ``startup_auth.bootstrap_github_auth_via_app(settings=..., repo=ctx.repo)``
    AFTER fetching the context (so ``ctx.repo`` is known) and BEFORE
    ``_execute``. The startup home-token bootstrap in ``__main__`` stays;
    this re-mint scopes the token to the task's repo so ``gh`` can clone /
    push outside the home installation.
    """
    from treadmill_agent.events import Artifact, StepOutput
    from treadmill_agent import startup_auth as startup_auth_mod

    ctx = _ctx(repo="owner/some-other-repo")

    call_order: list[tuple[str, dict[str, Any]]] = []

    def _fake_bootstrap(*, settings, repo=None):  # type: ignore[no-untyped-def]
        call_order.append(("bootstrap", {"repo": repo}))

    def _fake_execute(c, s):
        call_order.append(("_execute", {"repo": c.repo}))
        return StepOutput(
            summary="ok", decision="pushed", commit_sha="abc",
            artifacts=[Artifact(kind="branch", value="task/x")],
        ), None

    monkeypatch.setattr(
        startup_auth_mod, "bootstrap_github_auth_via_app", _fake_bootstrap,
    )
    monkeypatch.setattr(runner, "_execute", _fake_execute)

    sqs = _FakeSqs([_claim(
        ctx.step_id, task_id=ctx.task_id, plan_id=ctx.plan_id,
        run_id=ctx.run_id, receipt="rh-mint",
    )])
    pub = _FakePublisher()
    n = runner.run(
        settings=_settings(repo_mode="github", github_auth_mode="app"),
        api=_FakeApi(ctx),
        sqs_client=sqs,
        publisher=pub,
    )
    assert n == 1
    # Mint happened first, with ctx.repo; then _execute ran with the same repo.
    assert call_order == [
        ("bootstrap", {"repo": "owner/some-other-repo"}),
        ("_execute", {"repo": "owner/some-other-repo"}),
    ], call_order
    assert sqs.deleted == ["rh-mint"]


def test_handle_step_skips_repo_scoped_mint_when_not_github_app_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-task mint only fires for ``repo_mode='github'`` +
    ``github_auth_mode='app'``. Local mode (or PAT mode) leaves ``gh``
    alone — the startup bootstrap already configured it (or it's not used
    at all in local mode).
    """
    from treadmill_agent.events import Artifact, StepOutput
    from treadmill_agent import startup_auth as startup_auth_mod

    ctx = _ctx()
    mint_calls: list[dict[str, Any]] = []

    def _track_bootstrap(*, settings, repo=None):  # type: ignore[no-untyped-def]
        mint_calls.append({"repo": repo})

    monkeypatch.setattr(
        startup_auth_mod, "bootstrap_github_auth_via_app", _track_bootstrap,
    )
    monkeypatch.setattr(
        runner, "_execute",
        lambda c, s: (
            StepOutput(
                summary="ok", decision="pushed", commit_sha="abc",
                artifacts=[Artifact(kind="branch", value="task/x")],
            ),
            None,
        ),
    )

    sqs = _FakeSqs([_claim(ctx.step_id, receipt="rh-local")])
    pub = _FakePublisher()
    runner.run(
        settings=_settings(repo_mode="local"),  # default — no app-mode mint
        api=_FakeApi(ctx),
        sqs_client=sqs,
        publisher=pub,
    )
    assert mint_calls == []


def test_handle_step_publishes_failed_when_repo_scoped_mint_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0049 + the outage lesson: a per-task mint failure must publish
    ``step.failed`` (audit trail) and leave the SQS message in flight (per
    ADR-0025). ``_execute`` must NOT run if the mint failed.
    """
    from treadmill_agent import startup_auth as startup_auth_mod

    ctx = _ctx(repo="owner/forbidden-repo")
    execute_called: list[bool] = []

    def _boom_bootstrap(*, settings, repo=None):  # type: ignore[no-untyped-def]
        raise startup_auth_mod.StartupAuthError(
            f"installation not found for {repo}"
        )

    def _track_execute(c, s):
        execute_called.append(True)
        from treadmill_agent.events import StepOutput
        return StepOutput(summary="x", decision="pushed", commit_sha="a"), None

    monkeypatch.setattr(
        startup_auth_mod, "bootstrap_github_auth_via_app", _boom_bootstrap,
    )
    monkeypatch.setattr(runner, "_execute", _track_execute)

    sqs = _FakeSqs([_claim(
        ctx.step_id, task_id=ctx.task_id, plan_id=ctx.plan_id,
        run_id=ctx.run_id, receipt="rh-mint-fail",
    )])
    pub = _FakePublisher()
    with pytest.raises(startup_auth_mod.StartupAuthError, match="installation not found"):
        runner.run(
            settings=_settings(repo_mode="github", github_auth_mode="app"),
            api=_FakeApi(ctx),
            sqs_client=sqs,
            publisher=pub,
        )

    # _execute never ran — the mint failed first.
    assert execute_called == []
    # Audit trail: started, then failed with the mint error.
    names = [name for name, _ in pub.events]
    assert names == ["started", "failed"]
    assert "installation not found" in pub.events[1][1]["error"]
    # Queue-hygiene: SQS message stays in flight.
    assert sqs.deleted == []


def test_queue_hygiene_delete_strictly_inside_try_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structural test: ``_delete`` must run AFTER ``_handle_step`` returns
    normally, both inside the same try block, with no path between
    ``_handle_step`` raising and ``_delete`` being called.

    This proves the call order at the runner level rather than via a
    handler stub — if a future refactor moves the delete to a ``finally``
    or to the ``except`` branch, the assertions here catch it.
    """
    ctx = _ctx()
    call_order: list[str] = []

    def _spy_handle(*args, **kwargs):
        call_order.append("_handle_step")

    def _spy_delete(*args, **kwargs):
        call_order.append("_delete")

    monkeypatch.setattr(runner, "_handle_step", _spy_handle)
    monkeypatch.setattr(runner, "_delete", _spy_delete)

    sqs = _FakeSqs([_claim(ctx.step_id, receipt="rh-order")])
    pub = _FakePublisher()
    n = runner.run(
        settings=_settings(), api=_FakeApi(ctx),
        sqs_client=sqs, publisher=pub,
    )
    assert n == 1
    # Success path: handle first, then delete. Never the other order.
    assert call_order == ["_handle_step", "_delete"], call_order

    # And the failure path: when _handle_step raises, _delete is NEVER
    # entered.
    call_order.clear()
    def _spy_handle_raises(*args, **kwargs):
        call_order.append("_handle_step")
        raise RuntimeError("simulated mid-work failure")
    monkeypatch.setattr(runner, "_handle_step", _spy_handle_raises)

    sqs2 = _FakeSqs([_claim(ctx.step_id, receipt="rh-order-fail")])
    pub2 = _FakePublisher()
    with pytest.raises(RuntimeError, match="simulated mid-work failure"):
        runner.run(
            settings=_settings(), api=_FakeApi(ctx),
            sqs_client=sqs2, publisher=pub2,
        )
    assert call_order == ["_handle_step"], (
        f"_delete was called after _handle_step raised; order={call_order}"
    )
    assert sqs2.deleted == []
