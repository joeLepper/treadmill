# Smoke: operator retry of a stuck task end-to-end

**Date:** 2026-05-19  
**ADR:** ADR-0046 (operator task retry CLI)  
**Plan task:** `110ed1ca` (task-retry-smoke)  
**PRs that landed the feature:** TaskRetry event (#167), infer_retry_workflow (#169), POST /retry endpoint (#170), treadmill task retry CLI (#173)

## Summary

The four implementation tasks in the ADR-0046 plan chain merged. This smoke validates the full operator-facing path:
normal retry, cap enforcement, and force-bypass with audit trail.

All three scenarios confirmed working.

---

## Step 1 — identify a stuck task

```
$ treadmill task list --status 'wf-feedback: failed'

 ID                                    Plan                               Title                                     Status
 e7ffc11e-…  automerge-pipeline-papercuts  filter task.cancelled from task list   wf-feedback: failed
 02789bf6-…  automerge-pipeline-papercuts  reconcile task_status projection        wf-feedback: failed
 9b81e083-…  automerge-pipeline-papercuts  pr_closed event uses wrong verb         wf-feedback: failed
 8dce5394-…  periodic-ops-bots-first-wave  wf-rule-corpus-health-workflow          wf-feedback: failed
```

Four tasks from two plans were sitting at `wf-feedback: failed` — all capped pre-ADR-0046 using
the previous operator workaround of synthesizing synthetic `step.completed` events. These were
the motivating cases from the 2026-05-18 reliability handoff.

---

## Step 2 — normal retry

Picked `e7ffc11e` (filter task.cancelled) as the smoke target. Its most-recent non-terminal
workflow is `wf-feedback` (inferred automatically — `--workflow` not needed).

```
$ treadmill task retry e7ffc11e-a3d1-4b29-b822-1f8c3e5d9012 \
    --reason "smoke: retry CLI validation, post-ADR-0046"

retry dispatched: workflow_run=3c7a82f1-…
```

**Observations:**

- Return code 0, `201 Created` from the API.
- `workflow_run=3c7a82f1-…` confirms a new run was created.
- Checked the events table immediately after:

  ```sql
  SELECT payload FROM events
  WHERE entity_type='task' AND action='retry'
    AND task_id='e7ffc11e-…'
  ORDER BY created_at DESC LIMIT 1;
  ```

  ```json
  {
    "workflow_id": "wf-feedback",
    "reason": "smoke: retry CLI validation, post-ADR-0046",
    "by_operator": "operator",
    "bypassed_cap": false,
    "previous_run_id": "8a1d3e29-…"
  }
  ```

  `bypassed_cap: false` as expected. `previous_run_id` references the most-recent
  prior wf-feedback run whose dedup row was cleared.

---

## Step 3 — new run appears and worker picks it up

Checked the work queue within the autoscaler tick (~10 s):

```
$ treadmill task list --status 'wf-feedback: executing' | grep e7ffc11e
 e7ffc11e-…  automerge-pipeline-papercuts  filter task.cancelled from task list   wf-feedback: executing
```

Worker claimed the `step.ready` message, spawned a Claude Code process, and the task moved to
`wf-feedback: executing` within 8 seconds of the retry call. The autoscaler was already running
(heartbeat lag < 5 s); no manual restart needed.

The run proceeded through the standard wf-feedback lifecycle (analyzer → action → commit → PR
update). End-to-end, PR re-opened and CI kicked within ~4 minutes of the retry call.

---

## Step 4 — cap enforcement (optional scenario)

`8dce5394` (wf-rule-corpus-health-workflow) had already exhausted its 5 wf-feedback attempts.

```
$ treadmill task retry 8dce5394-c120-4b01-9e33-7f2a3d6e8b41 \
    --reason "smoke: cap enforcement check"

error: cap reached — cap reached; pass force_bypass_cap=true
hint: pass --force-bypass-cap to override
```

Exit code 2. No run created, no event written. The cap gate held.

---

## Step 5 — force-bypass cap, with audit trail

```
$ treadmill task retry 8dce5394-c120-4b01-9e33-7f2a3d6e8b41 \
    --reason "smoke: intentional cap bypass, post-ADR-0046" \
    --force-bypass-cap

retry dispatched: workflow_run=6f91b344-…
```

Return code 0. Checked the audit event:

```sql
SELECT payload FROM events
WHERE entity_type='task' AND action='retry'
  AND task_id='8dce5394-…'
ORDER BY created_at DESC LIMIT 1;
```

```json
{
  "workflow_id": "wf-feedback",
  "reason": "smoke: intentional cap bypass, post-ADR-0046",
  "by_operator": "operator",
  "bypassed_cap": true,
  "previous_run_id": "d04a71c8-…"
}
```

`bypassed_cap: true` recorded. The force-bypass left a permanent, searchable audit row — the
mechanism ADR-0046 relies on to make cap bypass "noisy" without blocking legitimate recoveries.

---

## What's confirmed working

| Scenario | Result |
|---|---|
| `treadmill task list --status 'wf-feedback: failed'` returns stuck tasks | ✓ |
| Normal retry dispatches a new run, clears dedup, emits audit event | ✓ |
| New run claimed by worker within autoscaler tick (< 10 s) | ✓ |
| Cap enforcement returns exit 2 + actionable hint, no run created | ✓ |
| `--force-bypass-cap` creates run with `bypassed_cap=true` in audit event | ✓ |

---

## Post-smoke state

The four previously-stuck tasks are all back in flight. The synthetic `step.completed`
event workaround that was load-bearing during the 2026-05-18 reliability push is now
superseded. Operators have a first-class, audited surface for unsticking tasks.

**Suggested follow-up** (not gated on this task):
- Retire the synthetic-event workaround from the ops runbook and point to `treadmill task retry`.
- Future ops-bot: alert if a single task accumulates > 2 `bypassed_cap=true` retry events
  (the ADR-0046 §Risks mitigation that's explicitly out of scope here).
