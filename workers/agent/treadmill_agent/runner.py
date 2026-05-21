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
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from treadmill_agent import claude_code, git, startup_auth, workspace
from treadmill_agent.api_client import ApiClient, WorkerContext
from treadmill_agent.config import Settings
from treadmill_agent.events import StepOutput
from treadmill_agent.eventbus import EventPublisher
from treadmill_agent.observability import extract_trace_context, get_tracer
from treadmill_agent.runner_dispositions import (
    handle_analysis,
    handle_architecture,
    handle_code,
    handle_crystallization,
    handle_documentation,
    handle_plan_doc,
    handle_review,
    handle_validation,
)
from treadmill_agent.runner_dispositions._context import DispositionContext

logger = logging.getLogger("treadmill.agent.runner")


# ADR-0025: per-message daemon heartbeat thread keeps the SQS visibility
# lease alive while the worker is processing. Treadmill cribs RAMJAC's
# `vllm_server/rpc_server.py` values verbatim — 30s interval, 120s
# extension. The base queue visibility is intentionally short (60s) so
# that a worker that *dies* (segfault, OOM, container exit) stops
# heartbeating; SQS expires the lease ~60s later; a fresh worker picks
# the message up. The heartbeat thread and the main thread share
# ``receipt_handle`` + ``sqs_client`` + ``queue_url`` — all read-only
# after creation; no concurrent mutation.
HEARTBEAT_INTERVAL_SECONDS = 30
VISIBILITY_EXTENSION_SECONDS = 120


def _run_heartbeat(
    sqs_client: Any,
    queue_url: str,
    receipt_handle: str,
    stop_event: threading.Event,
) -> None:
    """Extend message visibility every ``HEARTBEAT_INTERVAL_SECONDS`` until
    ``stop_event`` is set.

    RAMJAC pattern (line-for-line per ADR-0025): wait first so a fast
    worker that finishes inside the base visibility window never makes a
    visibility-extension API call. On AWS error, log a warning and keep
    looping — a panic-and-die would lose work-in-progress for transient
    hiccups; the lease will eventually expire and SQS will redeliver if
    the failures persist long enough.
    """
    while not stop_event.wait(HEARTBEAT_INTERVAL_SECONDS):
        try:
            sqs_client.change_message_visibility(
                QueueUrl=queue_url,
                ReceiptHandle=receipt_handle,
                VisibilityTimeout=VISIBILITY_EXTENSION_SECONDS,
            )
        except Exception:
            logger.warning(
                "Failed to extend message visibility timeout",
                exc_info=True,
            )


# Per ADR-0022 — dispatch table keyed by ``role.output_kind``. The
# runner's ``_execute`` runs the shared prefix (clone, checkout, Claude
# Code) then looks up the handler for the step's role's kind and
# delegates. Adding a future kind (e.g. when the Ralph-loop validation
# ADR lands) is a one-line schema change here plus the handler module.
DISPOSITIONS: dict[str, Callable[[DispositionContext], StepOutput]] = {
    "code": handle_code,
    "review": handle_review,
    "analysis": handle_analysis,
    "plan_doc": handle_plan_doc,
    "documentation": handle_documentation,
}


class UnknownOutputKindError(RuntimeError):
    """Raised when a step's role declares an ``output_kind`` the
    dispatch table doesn't recognize. A new kind on the API side that
    the worker hasn't been updated for triggers this — the operator
    sees a clean step.failed naming the offending kind."""


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
    trace_context: Any = None


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
        # ADR-0025: spawn a daemon heartbeat thread per in-flight message
        # (RAMJAC ``_process_message`` pattern). On success the main
        # thread deletes the SQS message in the try block. On worker
        # failure we deliberately do NOT delete — the lease expires, SQS
        # redelivers, and after ``maxReceiveCount`` the message DLQs.
        # The ``finally`` block always stops the heartbeat thread.
        stop_event = threading.Event()
        heartbeat = threading.Thread(
            target=_run_heartbeat,
            args=(
                sqs_client,
                settings.work_queue_url,
                claim.receipt_handle,
                stop_event,
            ),
            daemon=True,
        )
        heartbeat.start()
        # Queue-hygiene contract (ADR-0025, ADR-0048): we do NOT ack on
        # uncaught exception. ``_delete`` lives strictly inside the try
        # block AFTER ``_handle_step`` returns normally. Any path that
        # raises — subprocess crash, claude_code timeout, network error,
        # disposition raise, OOM-killed mid-execute — skips the delete and
        # re-raises. SQS visibility expiry (~60s) then redelivers the
        # message; after maxReceiveCount=5 it lands in the DLQ. This is
        # the recovery mechanism for ``validate-crash-no-retry`` /
        # ``review-crash-no-retry`` style dead-ends — they're queue
        # hygiene, not architecture. The ``finally`` only stops the
        # heartbeat thread; it must NEVER call delete_message.
        try:
            _handle_step(claim, settings, api, publisher)
            _delete(sqs_client, settings.work_queue_url, claim.receipt_handle)
        except Exception:
            logger.error(
                "unhandled error processing step %s; leaving message "
                "in flight for SQS redelivery / DLQ",
                claim.step_id,
                exc_info=True,
            )
            raise
        finally:
            stop_event.set()
            heartbeat.join(timeout=5)
        processed += 1
        if settings.exit_after_step:
            logger.info("processed %d step(s); exiting per EXIT_AFTER_STEP", processed)
            return processed


def _receive_one(sqs_client: Any, settings: Settings) -> _Claim | None:
    resp = sqs_client.receive_message(
        QueueUrl=settings.work_queue_url,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=settings.poll_wait_seconds,
        MessageAttributeNames=["All"],
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
    trace_context = extract_trace_context(msg.get("MessageAttributes", {}))
    return _Claim(
        step_id=step_id, task_id=task_id, plan_id=plan_id, run_id=run_id,
        receipt_handle=msg["ReceiptHandle"],
        trace_context=trace_context,
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
    execution. If ``fetch_step_context`` or ``_execute`` raises, we
    publish ``step.failed`` for the audit trail and then re-raise so the
    runner's caller leaves the SQS message in flight (ADR-0025's
    don't-delete-on-error semantics) — visibility expiry then redelivers
    or DLQs the message.
    """
    tracer = get_tracer("treadmill.worker.step")
    with tracer.start_as_current_span("treadmill.worker.step", context=claim.trace_context):
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
            raise
        logger.info(
            "fetched context: step=%s task=%s repo=%s role=%s model=%s",
            ctx.step_id, ctx.task_id, ctx.repo, ctx.role.id, ctx.role.model,
        )
        # ADR-0049: in app-mode the startup bootstrap mints a home-token; now
        # that ``ctx.repo`` is known, re-mint a token scoped to the task's
        # repo so ``gh`` can clone / push against repos outside the home
        # installation. A mint failure must fail the step cleanly (publish
        # ``step.failed``) — a per-task mint failure must NOT crash the worker
        # process and take down the loop (the outage lesson).
        if settings.repo_mode == "github" and settings.github_auth_mode == "app":
            try:
                startup_auth.bootstrap_github_auth_via_app(
                    settings=settings, repo=ctx.repo,
                )
            except Exception as exc:
                logger.exception(
                    "step %s failed minting repo-scoped GitHub App token",
                    ctx.step_id,
                )
                publisher.publish_step_failed(
                    task_id=ctx.task_id, plan_id=ctx.plan_id,
                    run_id=ctx.run_id, step_id=ctx.step_id,
                    error=str(exc),
                )
                raise
        try:
            output = _execute(ctx, settings)
        except Exception as exc:
            logger.exception("step %s failed during execution", ctx.step_id)
            publisher.publish_step_failed(
                task_id=ctx.task_id, plan_id=ctx.plan_id,
                run_id=ctx.run_id, step_id=ctx.step_id,
                error=str(exc),
            )
            raise
        publisher.publish_step_completed(
            task_id=ctx.task_id, plan_id=ctx.plan_id,
            run_id=ctx.run_id, step_id=ctx.step_id,
            output=output,
        )


def _execute(ctx: WorkerContext, settings: Settings) -> StepOutput:
    """Do the actual work for a step. Returns the uniform ``StepOutput``
    envelope (ADR-0012) that lands in ``step.output`` via the coordination
    consumer.

    Per ADR-0022 — refactored into a shared prefix (clone, checkout,
    Claude Code) plus a per-kind dispatch via ``DISPOSITIONS``. The
    handler for the role's ``output_kind`` decides what to do with the
    Claude Code result:

      * ``code``     — diff/commit/push/PR (today's behavior)
      * ``review``   — empty diff is success; post ``gh pr review``
      * ``analysis`` — empty diff is success; emit artifact for the
                       downstream step (ADR-0015 composition)
      * ``plan_doc`` — like code, diff confined to ``docs/plans/``

    Per ADR-0029 — when workflow_id == 'wf-validate', the runner skips
    the Claude Code prefix entirely and dispatches directly to the
    validation handler.

    The runner's exception layer wraps the entire call so any handler
    raise (e.g. ``CodeAuthorError`` for empty-diff code-kind,
    ``MissingContextError`` for review-kind against a no-PR task)
    becomes a clean ``step.failed`` event.
    """
    # Per ADR-0029: for wf-validate, skip Claude Code and dispatch directly
    if ctx.workflow_id == "wf-validate":
        branch = _branch_for_step(ctx)
        with workspace.workspace_for_step(settings.workspace_dir, ctx.step_id) as ws:
            repo_dir = git.clone(
                repo=ctx.repo,
                mode=settings.repo_mode,
                bare_repos_dir=settings.bare_repos_dir,
                workspace=ws,
            )
            git.checkout_branch(repo_dir, branch)

            from treadmill_agent.claude_code import CodeAuthorResult  # local import

            dry_run = _is_dry_run()
            disposition_ctx = DispositionContext(
                ctx=ctx,
                claude_result=CodeAuthorResult(summary=""),
                repo_dir=repo_dir,
                branch=branch,
                settings=settings,
                is_dry_run=dry_run,
            )
            return handle_validation(disposition_ctx)

    # Standard path for non-wf-validate workflows
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
            # ADR-0020 phase 2: tag every streamed line from the Claude
            # Code subprocess with the step's context so the operator can
            # filter ``docker logs`` (and, in phase 3+, Loki) by
            # ``task_id`` / ``step_id`` / ``role``. The fields land in
            # ``extra`` on each log record; the human-readable format
            # stays unchanged so ``docker logs -f`` is still legible.
            log_context = {
                "task_id": ctx.task_id,
                "step_id": ctx.step_id,
                "run_id": ctx.run_id,
                "plan_id": ctx.plan_id,
                "role": ctx.role.id,
                "model": ctx.role.model,
                "workflow": ctx.workflow_id,
            }
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
                log_context=log_context,
            ).summary

        # Per ADR-0022 — dispatch on role.output_kind to the right
        # handler. ``claude_code.CodeAuthorResult`` carries the summary;
        # we wrap it inline to keep the dispatch context small without
        # reaching for a Result type the dry-run path doesn't produce.
        from treadmill_agent.claude_code import CodeAuthorResult  # local import

        disposition_ctx = DispositionContext(
            ctx=ctx,
            claude_result=CodeAuthorResult(summary=summary),
            repo_dir=repo_dir,
            branch=branch,
            settings=settings,
            is_dry_run=dry_run,
        )
        # role-crystallization-judge routes to the crystallization handler
        # for CrystallizationVerdict envelope parsing (step 1 of
        # wf-crystallize-learning). role-architect from
        # wf-crystallize-learning routes to the same handler for the
        # rule-authoring step (step 2). Both branches on role.id /
        # workflow_id keep the output_kind schema stable.
        if ctx.role.id == "role-crystallization-judge":
            return handle_crystallization(disposition_ctx)
        if ctx.role.id == "role-architect" and ctx.workflow_id == "wf-crystallize-learning":
            return handle_crystallization(disposition_ctx)

        # ADR-0032 §wf-architecture-resolve: role-architect uses
        # output_kind=analysis but routes to a dedicated disposition
        # (architecture.py) for verdict-envelope parsing + downstream
        # dispatch hints. Other analysis roles (planner, ci-analyzer,
        # feedback-analyzer, conflict-analyzer, validator) flow through
        # handle_analysis as before. Branch on role.id rather than
        # adding a new output_kind — keeps the schema stable while
        # letting role-architect have its own routing logic.
        if ctx.role.id == "role-architect":
            return handle_architecture(disposition_ctx)
        try:
            handler = DISPOSITIONS[ctx.role.output_kind]
        except KeyError:
            raise UnknownOutputKindError(
                f"role {ctx.role.id!r} declares output_kind="
                f"{ctx.role.output_kind!r} which is not in the worker's "
                f"dispatch table; known kinds: {sorted(DISPOSITIONS)}"
            )
        return handler(disposition_ctx)


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
