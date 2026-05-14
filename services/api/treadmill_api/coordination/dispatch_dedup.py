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

  | workflow      | discriminator              |
  |---------------|----------------------------|
  | wf-review     | pr=<N>,sha=<head_sha>      |
  | wf-feedback   | review=<review_id>         |
  | wf-ci-fix     | check_run=<check_run_id>   |
  | wf-conflict   | pr=<N>,sha=<base_sha>      |
  | wf-author     | (opts out — None)          |
  | wf-plan       | (opts out — None)          |

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
    or ``wf-feedback:<repo>:validate-run=<run_id>`` (wf-validate failure trigger).

    Three trigger sources fire wf-feedback:

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

    Different namespaces (``review=`` vs ``review-run=`` vs ``validate-run=``)
    intentionally so trigger sources do not collide on the dedup table — if
    multiple sources fire against the same task, both/all wf-feedback runs
    are legitimate (different intent sources).
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


def _build_wf_doc_amend_key(payload: dict[str, Any]) -> str | None:
    """``wf-doc-amend:<repo>:docs-amend-run=<run_id>``
    (wf-validate ``docs-current-with-pr`` failure trigger).

    One doc-amend remediation per wf-validate run that fails the
    ``docs-current-with-pr`` check. ``docs_amend_run_id`` is the UUID
    of the wf-validate run that triggered the dispatch; using the
    validate run id as the discriminator ensures at most one wf-doc-amend
    is dispatched per validation run regardless of re-delivery.
    """
    repo = payload.get("repo")
    docs_amend_run_id = payload.get("docs_amend_run_id")
    if not repo or not docs_amend_run_id:
        return None
    return f"wf-doc-amend:{repo}:docs-amend-run={docs_amend_run_id}"


# Per-workflow dedup-key builders. Workflows not in this dict implicitly
# opt out (the helper treats a missing entry as "return None"). Per
# ADR-0026's table, wf-author and wf-plan have no natural dedup key:
#
#   * wf-author runs are dispatched per Task, with task-level dedup via
#     the existing ``tasks`` PK.
#   * wf-plan dispatches from ``plan_doc_merged`` events, with the
#     ADR-0021 handler already deduping by ``uuid5(repo:path@sha)``.
#
# So they intentionally do not appear here.
DEDUP_KEY_BUILDERS: dict[str, Callable[[dict[str, Any]], str | None]] = {
    "wf-review": _build_wf_review_key,
    "wf-feedback": _build_wf_feedback_key,
    "wf-ci-fix": _build_wf_ci_fix_key,
    "wf-conflict": _build_wf_conflict_key,
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
