"""Dispatch predicates for the ADR-0118 coordinator-router (Team A / bert).

Two decision functions the event->dispatch consumer (alan) calls before it does its single
idempotent dispatch. They are PURE cores over already-fetched facts (so every ADR-0118
success-criterion foil runs DB-free and deterministically), each with a thin async wrapper
that fetches the facts from the events log and calls the core.

- ``depends_on_satisfied`` / ``is_depends_on_satisfied`` — is every ``depends_on`` edge of a
  task satisfied? An edge is a TERMINAL fact in the append-only events log, so a satisfied
  edge LATCHES: it stays satisfied across the upstream task's own later rework (the fact row
  never disappears). Satisfaction is generation-independent; ``generation`` scopes only the
  DEPENDENT's dispatch idempotency, owned by the consumer, not this predicate.
- ``ci_ready`` / ``is_ci_ready`` — have the REQUIRED check contexts reached terminal + passing
  for a head? CHECK-only (never mergeable_state, which conflates required-review with
  required-check and would deadlock the router, whose re-eval IS the review). Non-required
  checks are ignored entirely — a non-required failure/neutral/skipped never blocks
  (the #22845 ``unstable`` case). The required-context SET is INJECTED, so the production
  source (branch-protection contexts vs a stored view) can change without touching this logic.

ADR-0118 SC2 (wake), SC4 (rollup). The single dispatch WRITE and the ``(task_id, generation)``
idempotency live in the consumer; these predicates only DECIDE.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from treadmill_api.models.event import Event

# ── depends_on ────────────────────────────────────────────────────────────────

# task.<uuid>.pr_merged | task.<uuid>.run.completed | task.<uuid>.step.<name>.completed
# (already substituted to UUIDs at plan registration — routers/plans.py `_DEP_RE`).
_EDGE_RE = re.compile(
    r"^task\.(?P<task_id>[0-9a-fA-F-]{36})\."
    r"(?P<rest>pr_merged|run\.completed|step\.(?P<step>[A-Za-z0-9._-]+)\.completed)$"
)


@dataclass(frozen=True)
class TerminalFact:
    """A terminal fact the upstream task has achieved, normalized from an event row.

    ``kind`` is the edge kind (``pr_merged`` | ``run.completed`` | ``step.completed``);
    ``name`` is the step name for a ``step.<name>.completed`` edge, else ``None``.
    """

    task_id: str
    kind: str
    name: str | None = None


def _edge_of(expression: str) -> TerminalFact:
    """Parse one ``depends_on`` expression into the TerminalFact it requires.

    Raises ``ValueError`` on a malformed expression — the grammar was validated at plan
    registration, so a malformed edge here is a corruption we must not silently pass.
    """
    m = _EDGE_RE.match(expression)
    if m is None:
        raise ValueError(f"malformed depends_on expression: {expression!r}")
    rest = m.group("rest")
    if rest.startswith("step."):
        return TerminalFact(m.group("task_id"), "step.completed", m.group("step"))
    return TerminalFact(m.group("task_id"), rest, None)


def depends_on_satisfied(expressions: Iterable[str], facts: Iterable[TerminalFact]) -> bool:
    """True iff EVERY ``depends_on`` edge is matched by a terminal fact (AND over edges).

    Vacuously True for a task with no dependencies. Because ``facts`` is drawn from the
    append-only events log, a matched edge cannot later un-match — the latching invariant.
    Generation does not appear here: a terminal edge, once satisfied, stays satisfied across
    the upstream's rework.
    """
    have = set(facts)
    return all(_edge_of(expr) in have for expr in expressions)


async def is_depends_on_satisfied(
    session: AsyncSession, task_id, generation: int | None = None
) -> bool:
    """Fetch the task's dependency edges + the upstream terminal facts, then decide.

    ``generation`` is accepted for call-site symmetry with the consumer's dispatch key but is
    NOT used in the satisfaction decision — terminal-edge satisfaction is generation-
    independent (a satisfied edge latches across upstream rework). The consumer owns the
    ``(task_id, generation)`` dispatch idempotency.
    """
    from treadmill_api.models.task import TaskDependency  # local: avoid import cycle

    dep_rows = await session.execute(
        select(TaskDependency.expression).where(TaskDependency.task_id == task_id)
    )
    expressions = [row[0] for row in dep_rows.all()]
    if not expressions:
        return True  # no dependencies: dispatchable immediately

    upstream_ids = {_edge_of(e).task_id for e in expressions}
    facts = await _terminal_facts_for(session, upstream_ids)
    return depends_on_satisfied(expressions, facts)


async def _terminal_facts_for(session: AsyncSession, upstream_ids: set[str]) -> list[TerminalFact]:
    """Read the terminal facts (pr_merged / run.completed / step.completed) achieved by the
    given upstream tasks from the append-only events log.

    NOTE (confirm with alan): ``pr_merged`` is a confirmed github event carrying the resolved
    ``task_id`` (webhooks/persist.py). ``run.completed`` / ``step.<name>.completed`` are the
    grammar's other two edges; post-ADR-0087 they are rare/dormant, so their exact
    (entity_type, action, name-source) mapping is flagged for confirmation — the pure core is
    already correct for all three; only this translation depends on the emit convention.
    """
    rows = await session.execute(
        select(Event.task_id, Event.entity_type, Event.action, Event.payload).where(
            Event.task_id.in_(upstream_ids),
            Event.action.in_(("pr_merged", "completed")),
        )
    )
    facts: list[TerminalFact] = []
    for task_id, entity_type, action, payload in rows.all():
        tid = str(task_id)
        if action == "pr_merged":
            facts.append(TerminalFact(tid, "pr_merged"))
        elif action == "completed" and entity_type in ("workflow_run", "run"):
            facts.append(TerminalFact(tid, "run.completed"))
        elif action == "completed" and entity_type == "step":
            name = (payload or {}).get("step_name") or (payload or {}).get("name")
            facts.append(TerminalFact(tid, "step.completed", name))
    return facts


# ── CI readiness ──────────────────────────────────────────────────────────────

# A REQUIRED check is ready only when terminal with a passing conclusion. Kept as a module
# constant so widening it (e.g. if a required neutral/skipped should count as passing) is a
# one-line, reviewable change.
PASSING_CONCLUSIONS: frozenset[str] = frozenset({"success"})


@dataclass(frozen=True)
class CheckResult:
    """A check's terminal outcome for a head. ``conclusion`` is the terminal GitHub
    conclusion (``success`` | ``failure`` | ``neutral`` | ``skipped`` | ...), or ``None`` if
    the check has NOT reached terminal yet.
    """

    context: str
    conclusion: str | None


def ci_ready(results: Iterable[CheckResult], required: Iterable[str]) -> bool:
    """True iff every REQUIRED context is terminal + passing for this head.

    Non-required checks are ignored entirely — a non-required failure, neutral, or skipped
    never blocks (the ``unstable`` case). A required context that is absent or non-terminal
    (``conclusion is None``) is NOT ready — but the router must not wait forever on a
    non-required one, which is why only ``required`` is consulted.

    Empty ``required`` → True (nothing gates). The wrapper must never pass an empty set to
    mean "could not determine the required set"; that is a fetch-layer failure, not readiness.
    """
    observed = {r.context: r.conclusion for r in results}
    return all(observed.get(ctx) in PASSING_CONCLUSIONS for ctx in required)


async def is_ci_ready(session: AsyncSession, head_sha: str, required: Iterable[str]) -> bool:
    """Fetch the terminal ``task.ci_result`` outcomes for EXACTLY this head, then decide.

    Head isolation is enforced here: only ``ci_result`` events whose ``commit_sha`` equals
    ``head_sha`` count, so a result for a superseded head can never mark the new head ready.
    Builds on the idempotent per-suite ``task.ci_result`` events (``ci_observer``); does not
    re-derive the rollup.
    """
    rows = await session.execute(
        select(Event.payload).where(
            Event.entity_type == "task",
            Event.action == "ci_result",
            Event.commit_sha == head_sha,
        )
    )
    results = [
        CheckResult(
            context=str((p or {}).get("app_slug") or ""),
            conclusion=(p or {}).get("conclusion"),
        )
        for (p,) in rows.all()
    ]
    return ci_ready(results, required)
