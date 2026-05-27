# Audit: validate.override surface for PR-B10

**Date:** 2026-05-27
**Scope:** Does ADR-0042's `validate.override` surface exist as a callable HTTP
endpoint the dashboard can invoke?  Answer: **No. The surface is internal-only.**

---

## What code paths handle validate.override today

`validate.override` is an internal Event row emitted by the coordination
consumer — not an operator-triggered action.

The emission path is:

1. **`consumer.py:479–482`** — inside `_handle_step_completed`, when an
   architect step arrives with `action='completed'`, the consumer calls
   `self._maybe_emit_validate_override(session, step_id, typed)`.

2. **`consumer.py:1550–1577`** — `_maybe_emit_validate_override` delegates to
   `triggers.maybe_emit_validate_override_on_architect_completion`.

3. **`triggers.py:1845–1965`** —
   `maybe_emit_validate_override_on_architect_completion` checks that the
   step belongs to `wf-architecture-resolve` and that the step-completed
   payload carries `payload.dispatch.validate_override == True` (set by the
   architect role when it verdicts `accept-as-is` on a validate-deadlock).
   If both conditions hold, it INSERTs a `validate.override` Event row with
   `entity_type='validate'`, `action='override'`, and
   `override_validate_check_ids` naming the failing checks the architect is
   waiving. The INSERT is idempotent via a deterministic UUIDv5 keyed on
   `(task_id, commit_sha)`.

4. **`events/validate.py:3–54`** — defines `ValidateOverride`, the typed
   payload envelope for the event row. No HTTP surface is referenced or
   implied.

**Trigger source:** The override is emitted only when `wf-architecture-resolve`
(the architect workflow) completes with `verdict='accept-as-is'` on a task
whose deadlock was a validate-fail. The architect workflow is dispatched by the
consumer itself (`consumer.py:467–470` dispatches it after a `step.completed`
from wf-validate or wf-review signals a potential deadlock). There is no code
path that allows an operator or dashboard client to dispatch
`wf-architecture-resolve` or emit `validate.override` directly.

## Why no external surface exists

A search across all 16 router files under
`services/api/treadmill_api/routers/` finds no endpoint that emits
`validate.override` or accepts an override payload:

- `tasks.py` — read-only GET endpoints; no override route
- `event_triggers.py` — internal webhook target; no validate-override handler
- All other routers — unrelated to validate or review override events

The plan document (`docs/plans/2026-05-26-treadmill-dashboard-v1.md`, line 168)
sketched a hypothetical `POST /api/v1/reviews/override { task_id, head_sha,
decision }` endpoint tagged "(ADR-0042 — confirm)". That endpoint was never
implemented; it appears only as a placeholder in the plan's API sketch, with
an explicit "confirm" caveat.

The plan's action-affordances table (line 147) likewise flags the dependency:
> Override review | ADR-0042 `validate.override` — **confirm exists before
> wiring** | review is `changes_requested` or `needs-more-info`

This audit is the confirmation step.

## Additional mismatch: validate vs. review override

The dashboard's `ActionBar` (`services/dashboard/src/pages/TaskDetail.tsx:1023`)
currently gates the `override·review` button on:

```ts
const needsReviewOverride = task.pr?.review_decision === 'changes_requested';
```

`validate.override` (ADR-0042) overrides the **validate** gate — it is emitted
when wf-validate returned `fail` and the architect accepted the task anyway. It
has no direct relationship to `review_decision === 'changes_requested'`. The
review-gate override is a separate event (`review.override`, migration 0016),
which is also internal-only and also has no HTTP endpoint. The button's render
condition conflates the two override domains.

## What would be needed to add a callable surface

Out of scope for this PR, but for completeness:

- A new ADR to decide whether and under what conditions an operator should be
  able to manually trigger `validate.override` without an architect verdict.
- A new `POST /api/v1/tasks/:id/override-validate` endpoint in the API (a new
  router file, e.g. `routers/dashboard/actions.py`).
- Auth / precondition design: which task states permit a manual override?
  (At minimum: the task must have a `validate.fail` event at HEAD for the
  current PR's HEAD SHA, and must not already carry a `validate.override` at
  that SHA.)
- The dashboard `useOverrideValidate` mutation hook in `src/api/queries.ts`.

None of the above exists today.

---

**Recommendation for PR-B10:** remove the `override·review` branch from
`ActionBar` (`TaskDetail.tsx:1055–1059` and the `needsReviewOverride` variable
at line 1023). The affordance has no backing surface.
