# ADR-0079: Dispatcher short-circuits `step.ready` on terminal task status

- **Status:** proposed
- **Date:** 2026-06-05
- **Related:** ADR-0029 (architect amend cap), ADR-0062 (operator escalation incidents), ADR-0074 (architect nothing-to-do short-circuit — sibling case), ADR-0058 (gate-broken verdict — pre-architect short-circuit)

## Context

A worker-merged PR's owning task transitions to a terminal status
(`pr_merged`, `cancelled`, `superseded`) when the merge / cancel /
supersede event lands. The task is logically done. In practice the
SQS lifecycle of in-flight workflow runs against that task does not
stop: any `step.ready` already in flight, or any redelivery of one
whose lease expires, gets picked up by a worker and dispatched.

The worker runs the step. For an `action`-type step that means a
full Claude Code session: clone, branch, prompt, model invocation,
output capture. The session finds nothing to do (the PR is merged;
the precondition the step was meant to remediate is satisfied) and
emits an empty diff. The runner publishes `step.failed`; the
`maybe_dispatch_terminal_step_failure_escalation` trigger
(ADR-0062 Step 1) sees a failing step with no further pending
steps on the run and fires
`task.escalated_to_operator{reason: terminal_step_failure,
gate_log_excerpt: "Claude Code produced no changes to commit"}`.

The operator's session sees the escalation, triages, finds nothing
actionable (the task is `pr_merged` — the work is done), ignores
it. The next `step.ready` for the same task fires within seconds.
The loop repeats until SQS visibility / DLQ semantics finally drop
the message hours later.

The 2026-06-05 ADR-0076 implementation shipped two PRs that fell
into this exact loop after merge. Measured directly off the
events table: PR A burned 6 post-merge Claude Code sessions /
~24K output tokens; PR B burned 9 sessions / ~62K output tokens
in its first hour after merge with the loop still active at the
time of this ADR. Combined ~$1.30 at Sonnet output pricing and
climbing — small per incident but the pattern fires on every
worker-merged task, and the operator-side noise is worse than
the dollar cost (each escalation arrives via the relay channel,
crowding genuine signal).

ADR-0074 closed an adjacent case at the architect layer:
`_short_circuit_nothing_to_do` short-circuits
`wf-architecture-resolve` when the precondition is already
merged. That fix doesn't catch THIS pattern because the loop
here is at the wf-author / `action`-step level, not
architect-amend.

The structural gap: the dispatcher assigns work to runs without
consulting the task's terminal state. A terminal task means "no
further work is wanted from this dispatch chain"; the dispatcher
should honor that.

## Decision

We added a single dispatcher-layer guard: when the coordination
consumer is about to assign a `step.ready` to a worker, it checks
the owning task's `status` first. If the status is in the closed
set of terminal values
(`pr_merged | cancelled | superseded | escalation_closed_terminal`)
the dispatcher **does not assign the step**. Instead it emits
`step.skipped{reason: task_terminal, terminal_status: <status>}`
and marks the step `status='skipped'`. The SQS message is acked
(deleted) so it doesn't redeliver.

The check fires at the latest moment before the assign — not at
event publish time, not at workflow run creation time — so a task
that transitions to terminal mid-workflow (the common case: PR
merged while a downstream step was still pending) is honored
without breaking the workflow-run state machine that put the step
there in the first place.

`step.skipped` is a new event action paired with the existing
`step.completed` / `step.failed` envelope. Same `task_id` /
`step_id` keys; payload carries the skip reason + the task
status that triggered it. Sweeps and dashboards treat
`skipped` as a fourth terminal step state — same as `completed`
without an output, doesn't fire escalation producers.

The guard does NOT fire for non-action workflow types whose
output is intentionally side-effect-free (`wf-validate`,
`wf-review`, `wf-analyze`). A read-only step against a merged
task is fine — no commit, no escalation, no real cost beyond the
session itself. We narrow the guard to `action`-typed
workflows (the wf-author family) where the empty-diff →
escalation loop is the failure mode. A separate ADR can widen
the guard later if observation shows read-only steps also waste
budget.

## Alternatives considered

- **Cancel the workflow_run when the task goes terminal.** Looks
  obvious; rejected because workflow_runs are immutable artifacts
  of the coordination consumer's projection — cancelling
  retroactively breaks audit invariants (`task.status='pr_merged'`
  with `workflow_run.status='running'` is a legitimate snapshot of
  "the merge happened mid-run, the run will resolve naturally").
  The dispatcher-side guard achieves the same operational outcome
  without retroactive state mutation.
- **Let the worker self-detect terminal state and emit a
  no-op.** Worker would need an API call to read `task.status`
  before every action step — same query the dispatcher would make,
  but pushed to a less-controlled layer with no central enforcement.
  Carla's ADR-0074 path is the analog at the architect role
  level; replicating it across every action role multiplies the
  failure modes.
- **Filter at the workflow_run state machine** so terminal-task
  events stop being projected into new steps. Rejected for the
  same reason as cancellation: the run projection is an audit
  trail. The right surface is the dispatcher; that's the only
  layer that decides whether SQS work gets assigned.
- **Raise the architect amend cap** so the loop bounds at a
  smaller number. Rejected because the loop is `wf-author` /
  `action`-step, not `wf-architecture-resolve`. The amend cap
  doesn't apply.
- **Live with it; the cost is small.** Rejected because the
  operator-side noise from each loop iteration is a real
  attention tax. The escalations land in the per-session relay
  (ADR-0071) at every fire. Closing the loop is cheaper than
  scaling operator attention to absorb it.

## Consequences

### Good

- Closes the entire class of empty-diff-on-merged-task escalation
  loops. Measured savings against ADR-0076 implementation alone:
  ~15 sessions / ~86K output tokens / ~$1.30, with the pattern
  observable on every worker-merged task going forward.
- Operator-side: routine escalations stop firing on completed
  work. The escalation channel signal-to-noise ratio improves
  meaningfully for the per-session relay (ADR-0071 `normal` log
  level).
- New `step.skipped` event gives sweeps + dashboards a clean
  signal to distinguish "no work was done because the task was
  terminal" from "work failed." Today both are
  `terminal_step_failure` — the new shape lets us strip out the
  noise without losing the actual failure population.
- Architecturally clean: dispatcher is the only seam that
  decides whether SQS work gets assigned, so the guard lives
  exactly where the decision is made.

### Bad / trade-offs

- New event action `step.skipped` to add to the event registry,
  payload schema, projection, and the dashboard's step-status
  derivation. Not a deep change but it touches multiple files —
  bounded by the existing `step.completed` / `step.failed`
  patterns.
- The narrowing to `action`-typed workflows (vs. all step types)
  is a judgment call that could be wrong. If read-only steps on
  terminal tasks also waste meaningful budget, we'll need a
  follow-up to widen the guard. Mitigation: log the skip count
  by workflow type so the data is visible.
- A dispatcher decision based on `task.status` reads the task
  row on every step assign. The query is cheap (PK lookup) and
  already cached in the consumer's session for related work,
  but it's a measurable change in the dispatcher's per-step
  cost. Acceptable: PK lookups on a row already in session
  cache are sub-millisecond.

### Risks

- **False-negative on tasks that should terminate but haven't
  yet updated.** A worker-merged PR with a delayed `pr_merged`
  event projection means the task.status query during the
  dispatcher race window could return `running` when the task
  is actually terminal. Mitigation: dispatcher already polls
  the latest task row in a fresh transaction; the
  delay-to-projection window is < 1s in practice. Acceptable
  for a feature that's a noise reducer, not a correctness gate.
- **Race with the architect-amend cap escalation.** The
  cap-reached producer fires when the loop counter hits 5; if
  the dispatcher short-circuits the 6th attempt, we still emit
  `task.cap_reached` (different event, different reason) — that
  path is wf-architecture-resolve, unaffected here. No
  interaction.
- **Sweep producers expect `step.completed` or `step.failed`
  on every step.** The new `step.skipped` action needs to be
  honored by every sweep that scans for "ran out of recovery"
  signals. Implementation plan must enumerate the producers
  and update each. Coverage check via `git grep
  "step.completed\\|step.failed"` in the consumer / producer
  layer.

## Diagram

```mermaid
sequenceDiagram
    participant SQS
    participant Dispatcher as Coordination consumer<br/>(dispatcher seam)
    participant TaskRow as tasks
    participant Step as workflow_run_steps
    participant Worker
    participant Relay as Per-session relay<br/>(ADR-0071)

    SQS->>Dispatcher: step.ready (task_id=X, step_id=Y, workflow_type=action)
    Dispatcher->>TaskRow: SELECT status FROM tasks WHERE id=X
    TaskRow-->>Dispatcher: status='pr_merged' (terminal)

    alt task status is terminal AND workflow_type is action
        Dispatcher->>Step: UPDATE status='skipped', skip_reason='task_terminal'
        Dispatcher->>SQS: ack/delete message (no redeliver)
        Dispatcher-->>Relay: step.skipped{reason: task_terminal, terminal_status}
        Note over Dispatcher: No worker assigned;<br/>no Claude session burned;<br/>no escalation fired.
    else task status running OR workflow_type is read-only
        Dispatcher->>Worker: assign step (existing path)
        Worker-->>Dispatcher: step.completed | step.failed (existing path)
    end
```

## Follow-ups

- **Widen the guard to read-only workflow types** if
  observation shows wf-validate / wf-review / wf-analyze on
  terminal tasks also burn meaningful budget. The narrowing
  here is deliberate; data should drive the widening.
- **Dashboard surface** for `step.skipped`: a small badge on
  the per-task step list distinguishing skipped from completed.
  Out of scope for this ADR — separate dashboard-track work.
- **Backfill** of the empty-diff loops that are still firing
  on already-merged tasks today (ADR-0076 PR A + PR B are
  active examples). Operator can run a one-shot SQL to mark
  the open workflow_runs as terminated; not blocking on the
  dispatcher guard but worth scheduling once the guard ships.

## References

- ADR-0029 — architect amend cap (the sibling-but-different
  bounding mechanism).
- ADR-0062 Step 1 —
  `maybe_dispatch_terminal_step_failure_escalation`; the
  producer this ADR is upstream of.
- ADR-0074 — `_short_circuit_nothing_to_do` at the architect
  layer; same shape, different layer.
- ADR-0058 — gate-broken verdict; pre-architect short-circuit
  pattern.
- Measured data: events table query 2026-06-05 against tasks
  `6af219e5` (ADR-0076 PR A) and `07a4ce22` (ADR-0076 PR B)
  showed 6 + 9 post-merge Claude sessions, 24K + 62K output
  tokens.
