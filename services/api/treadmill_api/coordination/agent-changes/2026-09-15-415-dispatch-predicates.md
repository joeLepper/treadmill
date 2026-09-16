# 2026-09-15 — ADR-0118 dispatch predicates (router Team A)

Adds `dispatch_predicates.py` — the two decision functions the ADR-0118 event→dispatch
consumer calls before its single idempotent dispatch:

- `depends_on_satisfied(expressions, facts)` / `is_depends_on_satisfied(session, task_id,
  generation=None)` — is every `depends_on` edge satisfied? An edge is a TERMINAL fact in the
  append-only events log (`pr_merged` / `run.completed` / `step.<name>.completed`), so a
  satisfied edge LATCHES across the upstream task's own later rework. Satisfaction is
  generation-independent; `generation` is accepted for call-site symmetry but not used in the
  decision (the consumer owns the `(task_id, generation)` dispatch idempotency).
- `ci_ready(results, required)` / `is_ci_ready(session, head_sha, required)` — have the
  REQUIRED check contexts reached terminal + passing for a head? CHECK-only (never
  `mergeable_state`, which conflates required-review with required-check and would deadlock
  the router, whose re-eval IS the review). Non-required checks are ignored — a non-required
  failure/neutral/skipped never blocks (the #22845 `unstable` case). The required-context set
  is INJECTED, so the production source can change without touching the logic. Builds on the
  idempotent per-suite `task.ci_result` events (`ci_observer`); head isolation is the
  `commit_sha == head_sha` filter.

Both are PURE cores over already-fetched facts (every ADR-0118 foil runs DB-free), plus a
thin async wrapper. Tests: `tests/test_dispatch_predicates.py` — RED-then-GREEN foils
including the latching edge, the step-name discrimination, the `unstable` non-required case,
and generation-independence; the naive impls were shown to go RED.

Open (flagged to alan): the `run.completed` / `step.<name>.completed` → event
(entity_type, action, name-source) mapping in `_terminal_facts_for` is best-effort — those
edges are rare/dormant post-ADR-0087; the pure core is correct for all three edge kinds, only
the wrapper translation depends on the emit convention. The required-context SOURCE
(branch-protection contexts vs a stored view) is the consumer's to wire; predicate is
source-independent.
