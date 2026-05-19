# ADR-0046: Operator task retry CLI

- **Status:** accepted (2026-05-18)
- **Date:** 2026-05-18
- **Related:** ADR-0026 (workflow-dispatch dedup), ADR-0029 (ralph-loop validation runner), ADR-0037 (author-side failures dispatch wf-feedback), ADR-0038 (deadlock arbitration), ADR-0042 (validate.override channel)

## Context

`wf-feedback` dispatch is gated by two complementary mechanisms:

1. **Dedup** (ADR-0026): `WorkflowDispatchDedup` keyed on
   `wf-feedback:<repo>:author-fail-run=<wf_author_run_id>` for the
   ADR-0037 author-fail path (and sibling keys for validate-fail and
   review-fail). The key is recorded synchronously with the dispatch
   so SQS re-delivery of the originating step.completed cannot
   produce a duplicate run.
2. **Cap** (`FEEDBACK_MAX_ATTEMPTS = 5`, per task): the per-task
   attempt count caps wf-feedback at five runs. Beyond the cap the
   task escalates to operator review — the intended architectural
   escape valve for runaway loops.

Both mechanisms work as designed. They also block any **operator-
driven retry** of a task that's stuck for transitory reasons:

- The original wf-author crashed silently (substrate bug like
  uv-not-found, since fixed) but the dedup row was written, so
  `_maybe_fire_author_feedback_on_step_failed` (added 2026-05-18 in
  PR #152) doesn't get a chance — dispatch dedup blocks the second
  attempt.
- The wf-feedback recovery cycle hit the 5x cap during a prompt-
  iteration window where the system was running with weaker prompts
  (pre-#150, pre-#151). The same task under the current prompts
  would likely succeed, but the cap is now load-bearing.
- A worker authored a buggy first attempt that the operator patched
  out-of-band. The task's dedup state still references the original
  wf-author run; a fresh dispatch would no-op.

The current operator workaround is to **synthesize a new
`step.completed` event** under a different step_id (which produces a
new run, bypassing dedup). This works but is fragile:

- Requires raw SQL or AWS CLI knowledge.
- Bypasses the dedup audit trail rather than honoring it.
- Doesn't increment the cap, so accidental re-fires can blow past
  the intended max.
- Has no operator-readable acknowledgement (no CLI surface).

Joe (2026-05-18): *"It's probably desirable to have a generalized
replay function in the CLI. But that's probably worth an ADR."*
This ADR is that.

## Decision

We add a `treadmill task retry <task-id>` CLI command (and supporting
API endpoint) that performs an **audited, gated, single-shot retry**
of a stuck task's most-recent terminal workflow.

### CLI surface

```
treadmill task retry <task-id>
    [--workflow wf-feedback|wf-author|wf-validate|...]
    [--reason "<one-line justification>"]
    [--force-bypass-cap]
```

- `task-id` — required; the task whose chain is stuck.
- `--workflow` — optional; the workflow to re-dispatch. Defaults to
  the most recent **non-terminal** workflow attempted, inferred
  from the run history. Explicit override required when the
  default would be ambiguous.
- `--reason` — required; free-text justification recorded on the
  audit event row.
- `--force-bypass-cap` — optional flag; required when the task is
  already at `FEEDBACK_MAX_ATTEMPTS` and the operator wants one more
  attempt. The flag is intentionally noisy — the cap exists for a
  reason, and bypassing it should be a deliberate operator choice.

### API endpoint

`POST /api/v1/tasks/{task-id}/retry` accepts a JSON body with the
fields above. Returns the new `workflow_run.id` on success or a
descriptive 4xx on gate failure (cap hit without `force`, no
applicable workflow inferable, task already terminal, etc.).

### Server-side behavior

On a valid request:

1. Resolve the target workflow (explicit or inferred).
2. Clear the matching `workflow_dispatch_dedup` row(s) for the
   task's most-recent run of that workflow. The dedup table grows
   one row per dispatch attempt; the retry clears the specific
   row(s) that would otherwise block the new dispatch.
3. Emit an audit event: `entity_type='task'`, `action='retry'`,
   payload `{workflow_id, reason, by_operator, bypassed_cap}`. The
   audit row is durable evidence of the manual intervention.
4. Persist + publish a fresh `step.ready` for the workflow's first
   step. Same path the normal dispatcher uses — same dedup
   namespace (newly cleared), same cap counting.
5. Workers pick up the new step via the standard SQS work-queue
   path; the rest of the lifecycle proceeds unchanged.

### Cap semantics

By default the cap is **respected**: if the task is already at 5
wf-feedback runs, the retry is refused with a clear error pointing
the operator at `--force-bypass-cap`. The flag exists so that
genuine recoveries (e.g. \"the cap fired during a prompt-iteration
window; current prompts will succeed\") are possible without
removing the cap entirely.

Each retry, whether normal or force-bypass, **does not
auto-increment** the cap count by extra: the new run counts as one,
the same as any natural dispatch. The cap is a count of attempts,
not a count of dispatches.

## Alternatives considered

- **No CLI; operator continues synthesizing events.** Rejected:
  fragile, bypasses dedup audit trail, no rate-limit on accidental
  re-fires. The pattern is real; it should have a first-class
  surface.
- **Auto-retry on cap hit when prompts change.** Rejected as too
  magical — the system can't reliably tell that current prompts
  would handle the failure differently. Explicit operator
  intervention is the right boundary.
- **Per-workflow cap reset rather than dedup clear.** Rejected as
  too coarse — resetting the cap also resets the audit count, and
  the audit count is load-bearing for the architecture's
  bounded-blast-radius property.
- **Cancel-and-resubmit pattern.** Rejected: the task's downstream
  dependency graph (other tasks depending on
  `task.<id>.pr_merged`) is keyed on the specific task ID. A new
  task ID would orphan all dependents.

## Consequences

### Good

- Operator has a first-class surface for unsticking tasks that
  the system's natural recovery can't reach.
- Synthetic `step.completed` event publishing as an operator
  workaround can be deprecated (captured in a follow-up learning).
- Audit trail is preserved: every retry produces a `task.retry`
  event row with `reason` and `bypassed_cap` fields. Future
  reviews can spot patterns (e.g., "task X required 3 retries —
  the system has a recurring failure mode here").
- Cap remains the load-bearing architectural escape valve; the
  retry flag is the **deliberate** override, not a silent bypass.

### Bad / trade-offs

- Adds an operator-facing surface that must be kept in sync with
  the dispatch machinery. Future workflow additions need to plumb
  through `--workflow` inference logic.
- `--force-bypass-cap` invites misuse. Mitigated by the reason
  field being required.

### Risks

- Operator retries a task that the system rightly capped because
  the work is genuinely impossible. The cap exists to catch this;
  bypass weakens it. **Mitigation:** the `reason` field is
  searchable in the audit table; periodic review surfaces
  patterns. If a task gets retried more than twice with
  `force-bypass-cap`, alert the operator team (out of scope here,
  filed as a future ops-bot).
- Dedup-row clearing is mutational. If the retry then fails to
  dispatch (e.g., SQS publish error), the dedup row is gone and a
  subsequent normal-path event could re-dispatch. **Mitigation:**
  the retry endpoint records the dedup-cleared event *before*
  attempting dispatch; the replay loop's existing
  `DispatchPublishFailed` recovery already handles the publish-
  failure case. The net invariant is preserved.

## Implementation scope (not part of this ADR — sequenced separately)

The implementation is mechanical given the design above:

1. Add `POST /api/v1/tasks/{task-id}/retry` to the tasks router.
   ~80 LOC + tests.
2. Add `treadmill task retry` to the CLI. ~50 LOC + tests.
3. Add `TaskRetry` event payload + register in events/registry.
   ~20 LOC.
4. Add the workflow-inference helper (find most-recent non-terminal
   workflow for a task). ~30 LOC + tests.

Filed as a follow-up plan once this ADR is accepted.

## Diagram

```mermaid
sequenceDiagram
    actor Operator
    participant CLI as treadmill CLI
    participant API as Treadmill API
    participant DB as Postgres
    participant SQS as Work queue
    participant W as Worker

    Operator->>CLI: task retry <id> --reason "..." [--force-bypass-cap]
    CLI->>API: POST /tasks/{id}/retry { workflow, reason, force }
    API->>DB: SELECT cap_count, last_workflow_run
    alt cap reached AND NOT force
        API-->>CLI: 409 cap reached; pass --force-bypass-cap
    else cap OK or force
        API->>DB: DELETE workflow_dispatch_dedup<br/>(target row)
        API->>DB: INSERT events (task.retry, reason, ...)
        API->>DB: INSERT workflow_runs + steps
        API->>SQS: send_message (step.ready)
        API-->>CLI: 201 { workflow_run_id }
        SQS-->>W: claim
        W->>API: GET /steps/{id}
        Note over W: lifecycle proceeds normally
    end
```

## Notes

- This ADR replaces the operator-side synthetic `step.completed`
  publishing pattern that's been used during the 2026-05-18
  hands-free push (PRs #149, #153, #156, #157 each required at least
  one synthetic event nudge). The pattern works but is fragile;
  ADR-0046 gives it a durable surface.
- The cap stays at 5. ADR-0029 / ADR-0038 picked that number for
  reasons that aren't changed by this ADR.
