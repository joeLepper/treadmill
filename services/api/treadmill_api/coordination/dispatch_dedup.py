"""Dispatch dedup builders + insert-first/dispatch-second helper per ADR-0026.

A single PR can fire repeated webhook events (``pull_request_review`` on
every comment, ``pull_request_synchronize`` on every push) that, without
dedup, spawn N redundant workflow runs against the same content. The
fix per ADR-0026 is a deterministic ``dedup_key`` built from the event
payload + a Postgres PK constraint enforcing single-dispatch.

This module owns two pieces:

  * ``DEDUP_KEY_BUILDERS`` — per-workflow lambdas mapping an event
    payload to its dedup key string (or ``None`` to opt out).
  * ``maybe_dispatch_with_dedup`` — the insert-first/dispatch-second
    helper. Inserts a placeholder dedup row inside a SAVEPOINT;
    on ``IntegrityError`` (concurrent or redundant dispatch) it
    skips. On success it calls ``dispatch_fn`` and UPDATEs the dedup
    row's ``workflow_run_id`` to the real run id.

Per ADR-0026 §"Optimistic pre-check + PK gate ordering" the ordering is:

  1. Build the dedup_key from the event payload + workflow_id.
  2. Insert the dedup row first (catch IntegrityError → skip).
  3. Only if the insert succeeded, call dispatch_fn() to create the run.
  4. UPDATE the dedup row's workflow_run_id to the real run's id.

This means we never create a workflow_run row that has to be rolled
back. If ``dispatch_fn`` raises, the SAVEPOINT rolls back the dedup
row too, so a retry will be allowed to land.

Per the ADR's "Discriminator parts" table:

  | workflow      | discriminator                       |
  |---------------|-------------------------------------|
  | wf-review     | pr=<N>,sha=<head_sha>               |
  | wf-feedback   | review=<review_id>                  |
  | wf-ci-fix     | check_run=<check_run_id>            |
  | wf-conflict   | pr=<N>,sha=<base_sha>               |
  | wf-auto-merge | auto-merge=<task_id>                |
  | wf-author     | supersede-parent=<parent_task_id>   |
  |               |   (else opts out — None)            |
  | wf-plan       | (opts out — None)                   |

Missing-field disposition: a builder that depends on a field the
normalizer does not currently emit (today: ``review_id``,
``check_run_id``, ``base_sha``) returns ``None`` — i.e. opts out
gracefully — until the normalizer is extended. The dedup table works
for the workflows whose discriminators *are* present (wf-review),
and the others fall through to unconditional dispatch (existing
behavior) until their payload fields land.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.models import WorkflowDispatchDedup

logger = logging.getLogger("treadmill.coordination.dispatch_dedup")


# Sentinel workflow_run_id written into the dedup row at insert time.
# UPDATEd to the real run's id once dispatch_fn returns. The zeros UUID
# is the universal "no run yet" marker — readable + unambiguous.
_SENTINEL_RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _build_wf_review_key(payload: dict[str, Any]) -> str | None:
    """``wf-review:<repo>:pr=<N>,sha=<head_sha>``.

    The review workflow evaluates the diff at a specific HEAD SHA; same
    SHA = identical content = no new review needed.
    """
    repo = payload.get("repo")
    pr_number = payload.get("pr_number")
    head_sha = payload.get("head_sha")
    if not repo or pr_number is None or not head_sha:
        return None
    return f"wf-review:{repo}:pr={pr_number},sha={head_sha}"


def _build_wf_feedback_key(payload: dict[str, Any]) -> str | None:
    """``wf-feedback:<repo>:review=<review_id>`` (human-submitted review)
    or ``wf-feedback:<repo>:review-run=<run_id>`` (wf-review self-trigger)
    or ``wf-feedback:<repo>:validate-run=<run_id>`` (wf-validate failure trigger)
    or ``wf-feedback:<repo>:author-fail-run=<run_id>`` (wf-author failure trigger).

    Four trigger sources fire wf-feedback:

      * ``pr_review_submitted`` webhook (human reviewer outside
        Treadmill) — payload carries ``review_id`` (GitHub node-id like
        ``PRR_kwDOSb...``). The normalizer does NOT emit this field
        today, so the human path falls through to unconditional
        dispatch.

      * ``wf-review.step.completed`` with ``decision='changes_requested'``
        (task #108 path 1 — Treadmill's own self-review fires this via
        ``maybe_dispatch_feedback_on_terminal_failure``).
        Payload carries ``review_run_id`` (UUID of the wf-review run).

      * ``wf-validate.step.completed`` with ``decision='fail'`` or ``'error'``
        (ADR-0029 — validation failure trigger via
        ``maybe_dispatch_feedback_on_terminal_failure``).
        Payload carries ``validate_run_id`` (UUID of the wf-validate run).

      * ``wf-author.step.completed`` with ``decision='fail'``
        (ADR-0037 — author failure trigger via
        ``maybe_dispatch_feedback_on_terminal_failure``).
        Payload carries ``author_run_id`` (UUID of the wf-author run).

    Different namespaces (``review=`` vs ``review-run=`` vs ``validate-run=`` vs
    ``author-fail-run=``) intentionally so trigger sources do not collide on the
    dedup table — if multiple sources fire against the same task, both/all wf-feedback
    runs are legitimate (different intent sources).
    """
    repo = payload.get("repo")
    if not repo:
        return None
    review_id = payload.get("review_id")
    if review_id:
        return f"wf-feedback:{repo}:review={review_id}"
    review_run_id = payload.get("review_run_id")
    if review_run_id:
        return f"wf-feedback:{repo}:review-run={review_run_id}"
    validate_run_id = payload.get("validate_run_id")
    if validate_run_id:
        return f"wf-feedback:{repo}:validate-run={validate_run_id}"
    author_run_id = payload.get("author_run_id")
    if author_run_id:
        return f"wf-feedback:{repo}:author-fail-run={author_run_id}"
    architect_amend_run_id = payload.get("architect_amend_run_id")
    if architect_amend_run_id:
        return f"wf-feedback:{repo}:architect-amend-run={architect_amend_run_id}"
    return None


def _build_wf_ci_fix_key(payload: dict[str, Any]) -> str | None:
    """``wf-ci-fix:<repo>:check_run=<check_run_id>``.

    One fix attempt per failing check_run. ``check_run_id`` is the
    GitHub check_run's id; the normalizer does NOT emit this field
    today, so this builder returns ``None`` for now.
    """
    repo = payload.get("repo")
    check_run_id = payload.get("check_run_id")
    if not repo or check_run_id is None:
        return None
    return f"wf-ci-fix:{repo}:check_run={check_run_id}"


def _build_wf_conflict_key(payload: dict[str, Any]) -> str | None:
    """``wf-conflict:<repo>:pr=<N>,sha=<base_sha>``.

    A conflict resolution depends on what main looks like; same base =
    same conflict = same resolution.

    The ``pr_conflict`` event emitted by the conflict-detection sweep
    carries ``head_sha`` (the conflicting PR's head), not ``base_sha``
    (the target's head). The base SHA is not currently captured, so
    this builder returns ``None`` for now; see module docstring.
    """
    repo = payload.get("repo")
    pr_number = payload.get("pr_number")
    base_sha = payload.get("base_sha")
    if not repo or pr_number is None or not base_sha:
        return None
    return f"wf-conflict:{repo}:pr={pr_number},sha={base_sha}"


def _build_wf_auto_merge_key(payload: dict[str, Any]) -> str | None:
    """``wf-auto-merge:<repo>:auto-merge=<task_id>``.

    One auto-merge dispatch per task. ``task_id`` is the UUID of the
    task whose PR is being merged; guarantees at most one auto-merge
    fires per task regardless of re-delivery or concurrent triggers.
    """
    repo = payload.get("repo")
    task_id = payload.get("task_id")
    if not repo or not task_id:
        return None
    return f"wf-auto-merge:{repo}:auto-merge={task_id}"


def _build_wf_doc_amend_key(payload: dict[str, Any]) -> str | None:
    """Two dispatch sources; namespace is determined by whichever
    discriminator field is present.

    ``docs-amend-run=<run_id>``
        wf-validate ``docs-current-with-pr`` failure trigger. One
        doc-amend per wf-validate run that fails the check.

    ``tune-rule=<rule_slug>``
        ADR-0040 architect validator-tuning trigger. One doc-amend per
        (repo, rule_slug) tuning proposal — prevents duplicate edits to
        the same rule YAML across re-deliveries of the same architect
        step.completed.

    Precedence: ``docs_amend_run_id`` wins when both are present
    (unexpected — defensive). Missing repo → None for both.
    """
    repo = payload.get("repo")
    if not repo:
        return None
    docs_amend_run_id = payload.get("docs_amend_run_id")
    if docs_amend_run_id:
        return f"wf-doc-amend:{repo}:docs-amend-run={docs_amend_run_id}"
    rule_slug = payload.get("rule_slug")
    if rule_slug:
        return f"wf-doc-amend:{repo}:tune-rule={rule_slug}"
    return None


def _build_wf_author_key(payload: dict[str, Any]) -> str | None:
    """``wf-author:<repo>:supersede-parent=<parent_task_id>``
    (ADR-0048 supersede trigger).

    The supersede trigger creates a child task and dispatches a fresh
    ``wf-author`` against it. The dedup key is keyed on the PARENT
    task id (the task being superseded), not the child — re-delivery
    of the same architect step.completed must not create N children
    against the same parent.

    Other wf-author dispatch paths (the natural one through
    ``dispatch_task`` for a freshly-registered task) opt out of dedup
    here: the ``tasks`` PK already provides task-level uniqueness for
    that path, and forcing a dedup key on it would interfere with the
    task-registration flow. So this builder only returns a key when
    the supersede discriminator is present.
    """
    repo = payload.get("repo")
    if not repo:
        return None
    supersede_parent_task_id = payload.get("supersede_parent_task_id")
    if supersede_parent_task_id:
        return f"wf-author:{repo}:supersede-parent={supersede_parent_task_id}"
    return None


def _build_wf_architecture_resolve_key(payload: dict[str, Any]) -> str | None:
    """Four dispatch sources; namespace determined by discriminator field.

    ``deadlock-feedback-run=<run_id>``
        ADR-0038 ralph-loop deadlock arbitration. One arbitration per
        wf-feedback run that resolved with ``responded-without-change``
        while a blocking gate (wf-review=changes_requested or
        wf-validate=fail) is still present. Distinct namespace from
        ADR-0032's ``class-c-learning`` trigger source.

    ``author-no-diff-run=<run_id>``
        ADR-0048 wf-author no-diff trigger. One arbitration per
        wf-author run that produced no changes to commit. The architect
        reviews the task spec (amend/supersede/accept-as-is).

    ``remote-rejected-run=<run_id>``
        ADR-0048 wf-author remote-rejection trigger. One arbitration per
        wf-author run whose git push was rejected (branch protection,
        stale force-with-lease, etc.). The architect almost always
        verdicts supersede to start fresh on a new branch.

    ``feedback-validation-fail-step=<step_id>``
        ADR-0048 follow-on (2026-05-19) wf-feedback validation-fail
        trigger. One arbitration per wf-feedback action step that
        committed a diff locally but had it rejected by author-side
        deterministic validation (``runner_dispositions/code.py``)
        before the push. Keyed on the wf-feedback action STEP id
        (not run id) because the action step is the one that produced
        the failure shape; a re-delivery of the same step.completed
        event must not double-dispatch.
    """
    repo = payload.get("repo")
    if not repo:
        return None
    deadlock_feedback_run_id = payload.get("deadlock_feedback_run_id")
    if deadlock_feedback_run_id:
        return (
            f"wf-architecture-resolve:{repo}:"
            f"deadlock-feedback-run={deadlock_feedback_run_id}"
        )
    author_no_diff_run_id = payload.get("author_no_diff_run_id")
    if author_no_diff_run_id:
        return (
            f"wf-architecture-resolve:{repo}:"
            f"author-no-diff-run={author_no_diff_run_id}"
        )
    author_remote_reject_run_id = payload.get("author_remote_reject_run_id")
    if author_remote_reject_run_id:
        return (
            f"wf-architecture-resolve:{repo}:"
            f"remote-rejected-run={author_remote_reject_run_id}"
        )
    feedback_validation_fail_step_id = payload.get(
        "feedback_validation_fail_step_id"
    )
    if feedback_validation_fail_step_id:
        return (
            f"wf-architecture-resolve:{repo}:"
            f"feedback-validation-fail-step={feedback_validation_fail_step_id}"
        )
    return None


# Per-workflow dedup-key builders. Workflows not in this dict implicitly
# opt out (the helper treats a missing entry as "return None").
#
#   * wf-author dispatches from ``dispatch_task`` per Task (no key
#     here — the ``tasks`` PK provides task-level dedup). But the
#     supersede trigger also dispatches wf-author against the child
#     task created from a parent task's verdict; that path keys on
#     ``supersede_parent_task_id`` to prevent re-delivery from
#     creating N children. See ``_build_wf_author_key``.
#   * wf-plan dispatches from ``plan_doc_merged`` events, with the
#     ADR-0021 handler already deduping by ``uuid5(repo:path@sha)``.
#     It opts out here.
DEDUP_KEY_BUILDERS: dict[str, Callable[[dict[str, Any]], str | None]] = {
    "wf-review": _build_wf_review_key,
    "wf-feedback": _build_wf_feedback_key,
    "wf-author": _build_wf_author_key,
    "wf-architecture-resolve": _build_wf_architecture_resolve_key,
    "wf-ci-fix": _build_wf_ci_fix_key,
    "wf-conflict": _build_wf_conflict_key,
    "wf-auto-merge": _build_wf_auto_merge_key,
    "wf-doc-amend": _build_wf_doc_amend_key,
}


def build_dedup_key(workflow_id: str, payload: dict[str, Any]) -> str | None:
    """Build the dedup key for a (workflow_id, payload) pair.

    Returns ``None`` for workflows that opt out of dedup OR for events
    whose required discriminator fields are missing. Callers that
    receive ``None`` should dispatch unconditionally (existing
    behavior).
    """
    builder = DEDUP_KEY_BUILDERS.get(workflow_id)
    if builder is None:
        return None
    return builder(payload)


async def maybe_dispatch_with_dedup(
    session: AsyncSession,
    *,
    workflow_id: str,
    payload: dict[str, Any],
    dispatch_fn: Callable[[], Awaitable[uuid.UUID | None]],
) -> uuid.UUID | None:
    """Insert-first/dispatch-second dedup wrapper per ADR-0026.

    Flow:

      1. Build the dedup_key. ``None`` → call ``dispatch_fn()``
         unconditionally (existing behavior preserved for workflows
         that opt out or for events with missing discriminator fields).
      2. Open a SAVEPOINT. INSERT a placeholder dedup row with a
         sentinel ``workflow_run_id``.
      3. On ``IntegrityError`` from the SAVEPOINT, log INFO + return
         ``None`` (no dispatch — another transaction already won).
      4. On success, call ``dispatch_fn()``. If it returns a run id,
         UPDATE the dedup row's ``workflow_run_id`` to that id. If
         ``dispatch_fn`` raises, the surrounding SAVEPOINT pattern
         rolls back the dedup row too so a retry can re-attempt.

    Returns the run id from ``dispatch_fn`` (or ``None`` if dedup
    skipped / dispatch_fn returned None).

    The SAVEPOINT is the transaction discipline that makes the
    insert-first ordering safe: if dispatch_fn raises *after* the
    dedup row insert, the savepoint rollback cleans up the dedup row
    inside the same outer transaction, leaving the surrounding session
    in a usable state for the caller's own rollback / commit.
    """
    dedup_key = build_dedup_key(workflow_id, payload)
    if dedup_key is None:
        # No dedup key — workflow opts out (or missing field). Fall
        # through to unconditional dispatch.
        return await dispatch_fn()

    # Insert-first inside a SAVEPOINT so an IntegrityError doesn't
    # poison the outer transaction.
    try:
        async with session.begin_nested():
            session.add(
                WorkflowDispatchDedup(
                    dedup_key=dedup_key,
                    workflow_run_id=_SENTINEL_RUN_ID,
                )
            )
            await session.flush()
    except IntegrityError:
        # Another transaction (or a prior delivery of the same event)
        # already inserted this dedup_key. Skip dispatch.
        logger.info(
            "dispatch_dedup: skipping duplicate %s dispatch for %s",
            workflow_id, dedup_key,
        )
        return None

    # Insert succeeded. Now dispatch. If dispatch_fn raises, the
    # caller's outer transaction will roll back (along with our
    # dedup row); we don't need a nested try here because the
    # SAVEPOINT has already committed and any error propagates.
    run_id = await dispatch_fn()

    # Backfill the real run_id onto the dedup row so operator queries
    # can join from dedup → workflow_runs. If dispatch_fn returned
    # None (e.g. no workflow version seeded), the dedup row stays
    # with the sentinel — that's fine for the dedup gate (the next
    # event with the same key still hits the PK collision and skips).
    if run_id is not None:
        await session.execute(
            update(WorkflowDispatchDedup)
            .where(WorkflowDispatchDedup.dedup_key == dedup_key)
            .values(workflow_run_id=run_id)
        )

    return run_id
