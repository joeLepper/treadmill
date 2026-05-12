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

    def receive_message(self, **kwargs) -> dict:
        if not self._claims:
            return {"Messages": []}
        return {"Messages": [self._claims.pop(0)]}

    def delete_message(self, *, QueueUrl: str, ReceiptHandle: str) -> None:
        self.deleted.append(ReceiptHandle)


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
    assert sqs.deleted == ["rh-1"]


def test_runner_publishes_failed_when_execute_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx()

    def _boom(*args, **kwargs):
        raise RuntimeError("git push rejected")
    monkeypatch.setattr(runner, "_execute", _boom)

    sqs = _FakeSqs([_claim(ctx.step_id, receipt="rh-1")])
    pub = _FakePublisher()
    runner.run(settings=_settings(), api=_FakeApi(ctx), sqs_client=sqs, publisher=pub)

    names = [name for name, _ in pub.events]
    assert names == ["started", "failed"]
    assert pub.events[1][1]["error"] == "git push rejected"
    # The claim is still deleted so it doesn't redeliver indefinitely;
    # a future ADR adds DLQs + retry policy. (Step.failed is in the
    # audit log so the user can still inspect.)
    assert sqs.deleted == ["rh-1"]


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
        )
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
        )
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
    # The claim is still consumed; the audit trail in events tells the
    # operator what went wrong.
    assert sqs.deleted == ["rh-1"]


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
    output = runner._execute(
        ctx,
        _settings(
            bare_repos_dir=str(bare_repos_dir),
            workspace_dir=str(workspace_dir),
        ),
    )

    assert isinstance(output, StepOutput)
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
    output = runner._execute(
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
    output = runner._execute(
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
    output = runner._execute(
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
    output = runner._execute(
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
    out = runner._execute(
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
    output = runner._execute(
        ctx,
        _settings(
            bare_repos_dir=str(bare_repos_dir),
            workspace_dir=str(workspace_dir),
        ),
    )

    assert "task_directive" not in output.payload, output.payload
