"""Retrospective role scorer — outcome signal from runtime data.

Per ADR-0056 (prompt tuning is role-agnostic via pluggable metrics),
the optimizer needs a scoring metric for non-judge roles (authors +
procedural). Judge roles already have one — verdict equality against a
gold corpus (``judge_eval.evaluate_judge_prompt``). Author + procedural
roles have no gold label; the signal we have is what happened
downstream after the role touched a task: did it ride cleanly to merge,
or did it trigger ``wf-feedback`` recovery loops?

``evaluate_role_retrospectively`` queries ``workflow_run_steps`` joined
to ``workflow_runs`` + ``tasks`` for the role's completed steps within
``window_seconds``, aggregates per task, and returns a
``RetroEvalResult`` with the same shape contract as ``EvalResult``
(``score`` in [-0.5, 1] + ``n`` + per-record detail).

``score = clean_fraction - 0.5 * looped_fraction`` per ADR-0056
§"Retrospective signal" — penalize loops more than rewards clean. A
"clean" task is ``merged AND runs <= 3``; a "looped" task is one with
``feedback_runs >= 1``. The same task can be both (mixed): a task
that hit wf-feedback once but still merged within the run cap is both
clean and looped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

# Threshold for the "clean" classification — a task counts as clean
# when the role touched it at most this many completed steps within the
# window AND the task's PR merged. Matches ADR-0056's "≤3 total runs".
_CLEAN_RUN_CAP = 3


@dataclass
class RetroEvalResult:
    score: float
    n: int
    per_task: list[dict]


_AGGREGATE_SQL = sa.text(
    """
    SELECT
      t.id AS task_id,
      COUNT(*) AS runs,
      SUM(CASE WHEN wr.trigger LIKE 'self:wf-feedback-%' THEN 1 ELSE 0 END)
        AS feedback_runs,
      SUM(CASE WHEN wr.trigger = 'self:architect-amend' THEN 1 ELSE 0 END)
        AS amend_runs,
      CASE WHEN EXISTS(
        SELECT 1 FROM events e
        WHERE e.task_id = t.id AND e.action = 'pr_merged'
      ) THEN 1 ELSE 0 END AS merged,
      SUM(COALESCE(s.input_tokens, 0) + COALESCE(s.output_tokens, 0))
        AS tokens
    FROM workflow_run_steps s
    JOIN workflow_runs wr ON wr.id = s.run_id
    JOIN tasks t ON t.id = wr.task_id
    WHERE s.role_id = :role_id
      AND s.status = 'completed'
      AND s.completed_at >= :cutoff
    GROUP BY t.id
    """
)


def evaluate_role_retrospectively(
    role_id: str,
    *,
    window_seconds: int,
    session: sa.orm.Session,
) -> RetroEvalResult:
    """Score a role's downstream outcomes over the last ``window_seconds``.

    For every task the role touched (a completed ``workflow_run_steps``
    row with ``role_id == role_id`` whose ``completed_at`` falls inside
    the window), aggregate:

      * ``runs``           — count of the role's completed steps on the task
      * ``feedback_runs``  — of those, how many ran under a
        ``self:wf-feedback-*`` trigger
      * ``amend_runs``     — ditto for ``self:architect-amend``
      * ``merged``         — does the task have a ``pr_merged`` event?
      * ``tokens``         — sum of (input + output) tokens across the
        role's steps on the task

    A task is "clean" iff ``merged AND runs <= 3``; "looped" iff
    ``feedback_runs >= 1``. Score is
    ``clean_fraction - 0.5 * looped_fraction`` over the population the
    role touched. Empty population returns ``score=0.0``.

    Args:
        role_id: e.g. ``"role-code-author"``.
        window_seconds: lookback window from "now" (UTC).
        session: SQLAlchemy sync session bound to an engine where the
            ``tasks`` / ``workflow_runs`` / ``workflow_run_steps`` /
            ``events`` tables exist. Tests inject an in-memory SQLite
            engine; production wires this to the API's Postgres.

    Returns:
        RetroEvalResult with the aggregate score, the touched-task
        count, and per-task detail dicts.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(seconds=window_seconds)

    rows = session.execute(
        _AGGREGATE_SQL, {"role_id": role_id, "cutoff": cutoff},
    ).all()

    per_task: list[dict] = []
    clean_count = 0
    looped_count = 0

    for row in rows:
        m = row._mapping
        runs = int(m["runs"])
        feedback_runs = int(m["feedback_runs"] or 0)
        amend_runs = int(m["amend_runs"] or 0)
        merged = bool(m["merged"])
        tokens = int(m["tokens"] or 0)

        per_task.append(
            {
                "task_id": str(m["task_id"]),
                "runs": runs,
                "feedback_runs": feedback_runs,
                "amend_runs": amend_runs,
                "merged": merged,
                "tokens": tokens,
            }
        )

        if merged and runs <= _CLEAN_RUN_CAP:
            clean_count += 1
        if feedback_runs >= 1:
            looped_count += 1

    n = len(per_task)
    if n == 0:
        score = 0.0
    else:
        score = (clean_count / n) - 0.5 * (looped_count / n)

    return RetroEvalResult(score=score, n=n, per_task=per_task)
