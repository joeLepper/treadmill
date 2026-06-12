# ADR-0091: Queue-driven single-active-team scheduler

- **Status:** proposed
- **Date:** 2026-06-12
- **Related:** ADR-0087 (long-lived team execution model), ADR-0089
  (token-economics controls), ADR-0090 (wake filtering — the
  parallel-preserving first lever this one backstops)

## Context

Even with wake filtering (ADR-0090) and worker→sonnet, *N* concurrent
software teams each running an opus coordinator + opus evaluator against a
single Claude subscription can exceed the 5-hour budget — the recurring
freeze on 2026-06-11→12. The operator directive (2026-06-12): when filtering
is not enough, run **one software team at a time**, selected by the plan/task
at the head of the queue. This is the durable long-term cap on concurrent
opus burn — a control plane, not a one-off manual pause (which the operator
did by hand on 2026-06-12: dropping to 2 workers/team and stopping units).

## Decision

A **team scheduler** keeps exactly one software team active at a time.

### 1. Queue-head → owning team

The active plan with the highest-priority pending work determines the owning
team. Plans already carry their repo / `coordinator_label`, so the head maps
to a repo-slug = a team. Selection applies **aging/fairness** so a
low-priority team cannot starve.

### 2. Team unit grouping

A team is the set of systemd units for a repo-slug
(`coordinator-<slug>`, `evaluator-<slug>`, `worker-<slug>-N`). A
`team-activate <slug>` operation starts that group and stops all other
software teams' groups. **Orchestrators** (`treadmill-alan`, …) and the
operator interface stay up always — they are the control plane and Joe's
surface, not software teams.

### 3. Reconciling supervisor

A single supervisor reacts to plan-lifecycle events (`plan.submitted`,
`plan.completed`, `plan.abandoned`, queue-drained) — recomputes the desired
active team and reconciles toward it. Idempotent; single-writer (one
instance decides) to avoid flapping.

### 4. Graceful pause + resume

A team is stopped only at a **safe point** — no task in `executing`; any
in-flight work finishes first. Stopped sessions `--resume` on reactivation,
so context is preserved across a pause (the launcher already persists the
per-label session id, ADR-0073). **Anti-flap hysteresis**: a minimum dwell
time per active team; no more than one swap per *X* minutes.

### 5. Clean stop semantics

Team units declare `SuccessExitStatus=143` (SIGTERM) so a scheduled pause is
not recorded as a unit failure — a papercut found 2026-06-12 (every
intentional `stop` currently lands the unit in `failed` until `reset-failed`).

## Consequences

- Concurrent opus burn is bounded to **one** team's coordinator + evaluator,
  regardless of how many teams have queued work.
- Teams make **serial** rather than parallel progress: throughput is traded
  for staying under the 5-hour cap. A paused team's plan waits its turn.
- Cross-team dependencies become queue-ordering concerns — a plan needing
  another repo's output waits until that team is scheduled.
- Risk: starvation (mitigated by aging in head-selection); flapping
  (mitigated by hysteresis); a team paused mid-thought losing momentum
  (mitigated by safe-point pause + `--resume`).

## Alternatives

- **Incumbent — all teams concurrent (ADR-0087), relying on ADR-0090
  filtering + sonnet to stay under budget.** This is the current model and
  the preferred default: parallel progress is faster. Keep it as default;
  activate the serial scheduler only under budget pressure. If ADR-0090's
  filtering proves sufficient, this ADR may stay `proposed` and unbuilt — it
  is explicitly the backstop, sequenced after measuring ADR-0090's effect.
- **Manual team pausing (operator stops units by hand).** What was done
  2026-06-12. Rejected as the durable answer: not queue-aware, doesn't
  scale, error-prone (left units in `failed`).
- **Multiple Claude accounts (fan the fleet across subscriptions to multiply
  the 5h budget).** Orthogonal: raises the ceiling rather than capping burn,
  and needs operator credential setup (only `~/.claude` + the expired
  `~/.claude-osmo` are logged in today, 2026-06-12). Could combine with this.
- **Scale workers to 2/team.** Already applied as a stopgap; low impact
  because workers are sonnet — the burn is the opus coordinator/evaluator.

## Out of scope

- The filter work (ADR-0090).
- Cross-repo dependency modeling beyond queue ordering.
- Multi-account fan-out.
- Scheduling the orchestrators or the operator surface — control plane stays
  always-on.
