"""Worker main loop.

One worker instance:

  1. Long-polls the SQS work queue.
  2. For each claim message, fetches the WorkerContext from the API.
  3. Publishes ``step.started``.
  4. Materializes a fresh workspace + clones the repo.
  5. Drives Claude Code against the repo using the role's config.
  6. Commits + pushes a branch, opens a PR (or records a local-mode
     branch reference).
  7. Publishes ``step.completed`` with the PR / branch metadata.
  8. On any failure, publishes ``step.failed`` with the error message.
  9. Deletes the SQS claim and — if ``EXIT_AFTER_STEP=true`` (the
     default) — exits. The autoscaler spawns a fresh replica per
     pending message.

This module is the orchestration of the per-module primitives in
``api_client``, ``eventbus``, ``git``, ``claude_code``, ``workspace``;
each of those is independently testable so this file stays a thin
shell with the lifecycle wiring.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from treadmill_agent import claude_code, git, workspace
from treadmill_agent.api_client import ApiClient, WorkerContext
from treadmill_agent.config import Settings
from treadmill_agent.events import Artifact, Metadata, StepOutput
from treadmill_agent.eventbus import EventPublisher

logger = logging.getLogger("treadmill.agent.runner")


@dataclass
class _Claim:
    """Parsed SQS claim message body.

    The dispatcher puts all four IDs in the JSON body so the worker can
    publish ``step.started`` *before* the API round-trip that fetches the
    full context. If the API is down, the worker still has enough to
    publish ``step.failed`` against the right run.
    """

    step_id: str
    task_id: str
    plan_id: str
    run_id: str
    receipt_handle: str


def run(
    *,
    settings: Settings,
    api: ApiClient,
    sqs_client: Any,
    publisher: EventPublisher,
) -> int:
    """Process messages and return how many were handled.

    When ``settings.exit_after_step`` is true (the default + the one-shot
    contract the autoscaler expects), the loop processes a single claim
    and returns. When false, the loop keeps polling — used for dev
    sessions where one worker stays attached to the queue.

    Returns 0 when the queue stays empty across a single long-poll cycle —
    matches the autoscaler's exit-cleanly contract for one-shot workers.
    """
    processed = 0
    while True:
        claim = _receive_one(sqs_client, settings)
        if claim is None:
            logger.info("queue empty after long-poll; exiting")
            return processed
        try:
            _handle_step(claim, settings, api, publisher)
        except Exception:
            logger.exception("unhandled error processing step %s", claim.step_id)
        finally:
            _delete(sqs_client, settings.work_queue_url, claim.receipt_handle)
        processed += 1
        if settings.exit_after_step:
            logger.info("processed %d step(s); exiting per EXIT_AFTER_STEP", processed)
            return processed


def _receive_one(sqs_client: Any, settings: Settings) -> _Claim | None:
    resp = sqs_client.receive_message(
        QueueUrl=settings.work_queue_url,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=settings.poll_wait_seconds,
    )
    msgs = resp.get("Messages", [])
    if not msgs:
        return None
    msg = msgs[0]
    try:
        body = json.loads(msg["Body"])
        step_id = body["step_id"]
        task_id = body["task_id"]
        plan_id = body["plan_id"]
        run_id = body["run_id"]
    except (KeyError, json.JSONDecodeError):
        logger.exception("malformed work-queue claim; deleting to prevent poison loop")
        _delete(sqs_client, settings.work_queue_url, msg["ReceiptHandle"])
        return None
    return _Claim(
        step_id=step_id, task_id=task_id, plan_id=plan_id, run_id=run_id,
        receipt_handle=msg["ReceiptHandle"],
    )


def _delete(sqs_client: Any, queue_url: str, receipt_handle: str) -> None:
    try:
        sqs_client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)
    except Exception:
        logger.exception("failed to delete sqs message; will be redelivered")


def _handle_step(
    claim: _Claim,
    settings: Settings,
    api: ApiClient,
    publisher: EventPublisher,
) -> None:
    """Run the full lifecycle for a single step.

    ``step.started`` is published *before* fetching the WorkerContext.
    The dispatcher already gave us all four IDs in the claim body, so a
    transient API outage no longer hides the fact that a step entered
    execution. If ``fetch_step_context`` raises, we publish
    ``step.failed`` with the error and bail.
    """
    publisher.publish_step_started(
        task_id=claim.task_id, plan_id=claim.plan_id,
        run_id=claim.run_id, step_id=claim.step_id,
    )
    try:
        ctx = api.fetch_step_context(claim.step_id)
    except Exception as exc:
        logger.exception("step %s failed during context fetch", claim.step_id)
        publisher.publish_step_failed(
            task_id=claim.task_id, plan_id=claim.plan_id,
            run_id=claim.run_id, step_id=claim.step_id,
            error=str(exc),
        )
        return
    logger.info(
        "fetched context: step=%s task=%s repo=%s role=%s model=%s",
        ctx.step_id, ctx.task_id, ctx.repo, ctx.role.id, ctx.role.model,
    )
    try:
        output = _execute(ctx, settings)
    except Exception as exc:
        logger.exception("step %s failed during execution", ctx.step_id)
        publisher.publish_step_failed(
            task_id=ctx.task_id, plan_id=ctx.plan_id,
            run_id=ctx.run_id, step_id=ctx.step_id,
            error=str(exc),
        )
        return
    publisher.publish_step_completed(
        task_id=ctx.task_id, plan_id=ctx.plan_id,
        run_id=ctx.run_id, step_id=ctx.step_id,
        output=output,
    )


def _execute(ctx: WorkerContext, settings: Settings) -> StepOutput:
    """Do the actual work for a step. Returns the uniform ``StepOutput``
    envelope (ADR-0012) that lands in ``step.output`` via the coordination
    consumer.

    Per ADR-0012's convention map for ``wf-author``:

    * ``summary``     - claude's headline (or dry-run marker note).
    * ``decision``    - ``pushed`` when the branch lands successfully.
                        ``no-changes`` is reserved for the explicit
                        ``CodeAuthorError`` path, which raises out of this
                        function and is mapped to ``step.failed`` at the
                        runner level. We do not emit a ``no-changes``
                        envelope from a successful path.
    * ``commit_sha``  - the per-execution anchor at top-level (ADR-0013
                        VIEW joins on this field).
    * ``artifacts``   - the branch and (when the PR opened) the PR URL.
    * ``payload``     - per-workflow extras: ``pr_number`` lives here by
                        convention (``None`` is encoded by *omitting* the
                        key for the local-bare-repo flow).
    * ``metadata``    - tokens / cost / duration left empty for v0; the
                        worker does not collect them yet.
    """
    branch = _branch_for_step(ctx)
    with workspace.workspace_for_step(settings.workspace_dir, ctx.step_id) as ws:
        repo_dir = git.clone(
            repo=ctx.repo,
            mode=settings.repo_mode,
            bare_repos_dir=settings.bare_repos_dir,
            workspace=ws,
        )
        git.checkout_branch(repo_dir, branch)

        dry_run = _is_dry_run()
        if dry_run:
            summary = _dry_run_author(repo_dir, ctx)
        else:
            summary = claude_code.run_claude_code(
                repo_dir=repo_dir,
                role=ctx.role,
                task_title=ctx.title,
                task_description=ctx.description,
                plan_intent=ctx.plan_intent,
                # Multi-step workflows (ADR-0015): the action role
                # reads the prior analyzer's ``task_directive`` via
                # ``prior_steps[-1].output.payload.task_directive``.
                # ``_compose_prompt`` folds the directive into the
                # prompt when this list is non-empty; single-step
                # runs pass through with an empty list.
                prior_steps=ctx.prior_steps,
            ).summary

        # Stage first, check second, decide-to-abort third. The dry-run
        # marker file *is* a staged change, so the no-changes check is
        # only meaningful for real Claude — otherwise a successful dry
        # run would always be aborted.
        git.stage_all(repo_dir)
        if not dry_run and not git.has_staged_changes(repo_dir):
            raise claude_code.CodeAuthorError(
                "Claude Code produced no changes to commit"
            )

        commit_message = _commit_message(ctx)
        commit_sha = git.commit_all(repo_dir, commit_message)
        git.push_branch(repo_dir, branch)
        pr_number, pr_url = git.open_pr(
            repo_dir=repo_dir, branch=branch,
            title=ctx.title, body=summary or ctx.title,
            repo=ctx.repo, mode=settings.repo_mode,
        )

    artifacts: list[Artifact] = [Artifact(kind="branch", value=branch)]
    if pr_url:
        artifacts.append(Artifact(kind="pr_url", value=pr_url))
    payload: dict[str, Any] = {}
    if pr_number is not None:
        payload["pr_number"] = pr_number
    # Multi-step dry-run support (ADR-0015): when the dry-run path runs an
    # analyzer step (step 1 of a 2-step workflow), the action step downstream
    # reads ``prior_steps[-1].output.payload.task_directive``. The real-Claude
    # path's prompt instructs the analyzer to emit a directive; the dry-run
    # path bypasses the LLM entirely, so we synthesize a minimal directive
    # here so the cross-step handoff is exercisable end-to-end.
    #
    # We detect "analyzer step" via the role id alone — every analyzer role
    # in ``starters.py`` ends in ``-analyzer`` (``role-feedback-analyzer``,
    # ``role-ci-analyzer``, ``role-conflict-analyzer``) or is the planner
    # (``role-planner``). The action role (``role-code-author`` /
    # ``role-doc-author``) doesn't match either condition so its payload is
    # unaffected.
    if dry_run and _is_analyzer_role(ctx.role.id):
        payload["task_directive"] = _dry_run_task_directive(ctx)
    return StepOutput(
        summary=summary,
        decision="pushed",
        commit_sha=commit_sha,
        artifacts=artifacts,
        payload=payload,
        metadata=Metadata(),
    )


def _is_dry_run() -> bool:
    """Set ``TREADMILL_AGENT_DRY_RUN=1`` in the environment to skip the
    Claude Code call and write a deterministic file instead. The smoke
    test + CI pipelines flip this on so they don't hit real LLMs.
    Production workers leave it unset."""
    import os
    return os.environ.get("TREADMILL_AGENT_DRY_RUN") == "1"


def _dry_run_author(repo_dir: Any, ctx: WorkerContext) -> str:
    """Stub authoring path: writes a deterministic file in the repo so
    ``git commit`` has something to record. Used only when dry-run mode
    is enabled (see ``_is_dry_run``)."""
    target = repo_dir / ".treadmill" / f"{ctx.step_id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"# Dry-run output for step {ctx.step_id}\n\n"
        f"Task: {ctx.title}\n"
        f"Plan intent: {ctx.plan_intent or '(none)'}\n"
        f"Workflow: {ctx.workflow_id} v{ctx.workflow_version}\n"
    )
    return f"dry-run: wrote {target.relative_to(repo_dir)}"


def _is_analyzer_role(role_id: str) -> bool:
    """Return True for analyzer roles in the eight-role taxonomy
    (ADR-0015 §"Role taxonomy"). The action roles (``role-code-author``,
    ``role-doc-author``) return False so their dry-run payloads stay
    unchanged from the single-step shape.

    Match by suffix + the explicit ``role-planner`` exception (the planner
    is the analyzer step of ``wf-plan`` but doesn't end in ``-analyzer``).
    """
    return role_id.endswith("-analyzer") or role_id == "role-planner"


def _dry_run_task_directive(ctx: WorkerContext) -> dict[str, Any]:
    """Synthesize a minimal ``task_directive`` for the dry-run analyzer
    path so the downstream action role's ``prior_steps[-1].output.payload
    .task_directive`` is non-empty (ADR-0015's analyzer→action contract).

    The shape mirrors ADR-0015's ``TaskDirective`` convention + ADR-0010's
    ``TaskSpec`` (same shape). We write a directive that, if followed
    literally, would have the action role touch a file under
    ``.treadmill/`` — a path the action role's dry-run also targets, so
    the test can assert the cross-step handoff fired without depending on
    a real LLM following the directive.
    """
    return {
        "summary": f"Dry-run analyzer directive for {ctx.workflow_id}",
        "files": [f".treadmill/{ctx.step_id}-directive.md"],
        "intent": (
            f"Address the {ctx.workflow_id} signal by writing a marker file. "
            f"Dry-run path: this directive is synthesized for the smoke test."
        ),
        "out_of_scope": [],
        "validation": [],
    }


def _branch_for_step(ctx: WorkerContext) -> str:
    """``task/<short-task-id>-<slugified-title>`` per ADR-0010's branch
    conventions table.

    The slug carries the human-readable hint; the short ID makes lookup
    mechanical. Step name is intentionally absent — at v0 every task is
    a single step (ADR-0010 §"Branch conventions"); multi-step reuse
    lands via B.3 (redelivery-safe ``git checkout -B``) in Phase 2.
    """
    short = ctx.task_id.replace("-", "")[:8]
    slug = _slugify_title(ctx.title)
    return f"task/{short}-{slug}"


_SLUG_MAX_LEN = 40
_SLUG_REPLACE_RE = re.compile(r"[^a-z0-9]+")


def _slugify_title(title: str) -> str:
    """Produce a filesystem- and shell-safe slug for the branch suffix.

    Rules:
      * lowercase
      * runs of non-``[a-z0-9]`` collapse to a single ``-``
      * strip leading / trailing ``-``
      * truncate to 40 chars on a word boundary (cut at the last ``-``)
      * empty / all-punctuation input → ``untitled``

    The output is restricted to ``[a-z0-9-]``, which means no path
    traversal (``..``, ``/``) and no shell metacharacters can survive.
    """
    lowered = title.lower()
    replaced = _SLUG_REPLACE_RE.sub("-", lowered)
    stripped = replaced.strip("-")
    if not stripped:
        return "untitled"
    if len(stripped) <= _SLUG_MAX_LEN:
        return stripped
    truncated = stripped[:_SLUG_MAX_LEN]
    # Cut at the last word boundary so we don't bisect a token; if the
    # truncated string has no ``-`` we just take the hard cut.
    last_dash = truncated.rfind("-")
    if last_dash > 0:
        truncated = truncated[:last_dash]
    return truncated or "untitled"


def _commit_message(ctx: WorkerContext) -> str:
    """Concise commit message — title only, with a trailer linking back
    to the task. The PR body carries the longer summary."""
    title = ctx.title.strip().splitlines()[0]
    return (
        f"{title}\n\n"
        f"Treadmill-Task-Id: {ctx.task_id}\n"
        f"Treadmill-Step-Id: {ctx.step_id}\n"
    )
