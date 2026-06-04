# Plan: ADR-0063 — Lazy-reconciliation cache for webhook FK resolution (implementation)

- **Status:** completed
- **Date:** 2026-05-29
- **Related ADRs:** ADR-0063 (the decision), ADR-0007 (existing
  cache-then-heal we generalize), ADR-0049 (App-identity ingress
  cutover that introduced the drift), ADR-0011 (single-writer
  invariant on `events.task_id`)

## Goal

Implement the four-part contract from ADR-0063 in concrete code.
Fix the SQS dual-ingress drift first so tonight's race class can
never recur on the SQS path, then generalize the key shape so
future FK families plug in without re-touching the helper module,
then centralize the buffer/no-buffer decision behind a shared
helper, then add a CI lock-step gate that flags any new ingress
that bypasses the helper.

Auto-merge enabled (default): the `depends_on` chain already
serializes touch-overlapping files (Step 2 + Step 3 both edit the
two ingress paths, but Step 3 only fires after Step 2's PR
merges), and the surface is isolated from the other in-flight
operator track.

## Success criteria

1. `coordination/webhook_inbox.py` calls `buffer_pending_event` on
   `task_prs` miss for github PR events, matching the HTTP route at
   `routers/webhooks.py:223-235`. An integration test pins that the
   buffer fires on missing-FK and the existing drain at
   `coordination/consumer.py:935` resolves it.
2. `webhooks/pending_events.py` exposes `buffer_pending_event`,
   `drain_pending_events`, and `pending_event_count` with an
   opaque `pending_buffer_key: str` parameter. A
   `pr_pending_buffer_key(repo, pr_number)` helper in the same
   module derives the existing `pr:<repo-lower>:<pr_number>:pending_events`
   shape. All current call sites use the helper.
3. A new `webhooks/persist.py` module exposes
   `persist_and_resolve_webhook_event(...)` that does the FK
   lookup, the buffer-on-miss, and the Event INSERT in one place.
   Both ingress paths (`routers/webhooks.py` + `coordination/
   webhook_inbox.py`) route through it.
4. A CI gate test fails if a route or coordination module persists
   an Event row outside `webhooks/persist.py` (grep-based; tight
   allowlist for the existing non-webhook writers).
5. `services/api/AGENT.md` documents the helper + the lock-step
   requirement, per ADR-0030.

## Constraints / scope

### In scope

- `coordination/webhook_inbox.py` — buffer-on-miss + later
  refactor through the shared helper.
- `webhooks/pending_events.py` — generalize key parameter +
  PR-bound key helper.
- New `webhooks/persist.py` — the shared helper.
- `routers/webhooks.py` + `coordination/consumer.py` — caller
  updates.
- New CI gate test for ingress lock-step.
- Unit + integration tests for each change.
- `AGENT.md` updates per ADR-0030.

### Out of scope

- The `(repo, head_sha)` and `(repo, branch)` FK families. ADR-0063
  says "add when first consumer ships"; we do not speculatively
  build them.
- Scheduler-side races (separate ADR per ADR-0063's out-of-scope
  clause).
- Backfilling existing NULL-`task_id` events on main. These remain
  manually backfilled as needed; future drain runs handle new
  cases.
- Operationalizing observability for orphaned Redis buffers
  (the 48h TTL handles them; richer metrics are a follow-up).

### Budget

Four worker dispatches, one per task. If any task wedges at the
architect-amend cap, the wedge is investigated before the next
ships.

## Sequence of work

```yaml
sequence_of_work:
  - id: webhook-inbox-buffer-on-miss
    title: "ADR-0063 Step 1 — mirror buffer call into the SQS ingress"
    workflow: wf-author
    intent: |
      STUDY: read these as shape references —
        - `services/api/treadmill_api/routers/webhooks.py` lines 193-240 —
          the HTTP route's task_prs lookup + buffer-on-miss + Event INSERT
          sequence. This is the source pattern.
        - `services/api/treadmill_api/coordination/webhook_inbox.py` lines
          498-528 — the SQS ingress that drifted. The comment at 498-504
          even cites the dual-ingress lock-step requirement.
        - `services/api/treadmill_api/webhooks/pending_events.py` — the
          buffer / drain helpers. No signature change in this task.

      BUILD: in `coordination/webhook_inbox.py`, after the task_prs
      lookup miss (where `task_id` resolves to None for a github event
      that has `repo` + `pr_number`), call `buffer_pending_event` with
      the same key shape the HTTP route uses. The buffer call is
      guarded by `self.redis_client is not None` (mirror the HTTP
      route's `request.app.state.redis is not None` check). Log the
      buffer at INFO so the operator can confirm the path took effect.

      Update the existing integration test
      `services/api/tests/test_integration_webhook_inbox.py` (or the
      sibling `test_webhook_inbox_unit.py` if the integration shape
      is too heavy) with a case that drives an SQS message for a
      `pr_opened` with no matching task_prs row, then asserts the
      Redis buffer key is non-empty and a subsequent task_prs INSERT
      via `coordination/consumer.py`'s back-fill path drains the buffer
      and sets `events.task_id` correctly.

      Update `services/api/AGENT.md`'s "Recent changes" with one
      bullet referencing ADR-0063 + this step.
    scope:
      files:
        - services/api/treadmill_api/coordination/webhook_inbox.py
        - services/api/tests/test_integration_webhook_inbox.py
        - services/api/tests/test_webhook_inbox_unit.py
        - services/api/AGENT.md
      services_affected:
        - services/api
      out_of_scope:
        - services/api/treadmill_api/routers/webhooks.py
        - services/api/treadmill_api/webhooks/pending_events.py
    validation:
      - kind: deterministic
        description: |
          The integration / unit tests touching webhook_inbox pass.
        script: |
          cd services/api && uv run pytest tests/test_integration_webhook_inbox.py tests/test_webhook_inbox_unit.py -q
      - kind: deterministic
        description: |
          webhook_inbox.py now imports + calls buffer_pending_event.
        script: |
          grep -lE "buffer_pending_event" services/api/treadmill_api/coordination/webhook_inbox.py
      - kind: deterministic
        description: |
          AGENT.md references ADR-0063.
        script: |
          grep -lE "ADR-0063" services/api/AGENT.md

  - id: generalize-pending-events-key
    title: "ADR-0063 Step 2 — generalize the buffer key parameter"
    workflow: wf-author
    depends_on: [task.webhook-inbox-buffer-on-miss.pr_merged]
    intent: |
      STUDY:
        - `services/api/treadmill_api/webhooks/pending_events.py` —
          the current implementation with `buffer_key(repo, pr_number)`
          hard-coded for the PR-bound shape.
        - All current callers (after Step 1):
          - `services/api/treadmill_api/routers/webhooks.py`
          - `services/api/treadmill_api/coordination/consumer.py`
            (two call sites: ~line 935 and ~line 1412)
          - `services/api/treadmill_api/coordination/webhook_inbox.py`

      BUILD: refactor `buffer_pending_event`, `drain_pending_events`,
      and `pending_event_count` to accept an opaque
      `pending_buffer_key: str` parameter in place of `repo` and
      `pr_number`. Keep the function bodies otherwise unchanged.

      Add a new helper `pr_pending_buffer_key(repo: str, pr_number:
      int) -> str` in the same module that returns
      `f"pr:{repo.lower()}:{pr_number}:pending_events"`. All four
      caller sites derive the key via the helper.

      Existing tests in `tests/test_pending_events.py` (if present)
      should pass against the refactored API. Add unit tests for the
      new helper + the opaque-key API (one positive case per function
      with a synthetic key like `"test:abc:pending_events"`).

      Update `services/api/AGENT.md`'s key-surfaces entry for
      `webhooks/pending_events.py` to document the opaque-key
      signature + the PR-bound helper, plus a "Recent changes" bullet.
    scope:
      files:
        - services/api/treadmill_api/webhooks/pending_events.py
        - services/api/treadmill_api/routers/webhooks.py
        - services/api/treadmill_api/coordination/consumer.py
        - services/api/treadmill_api/coordination/webhook_inbox.py
        - services/api/tests/test_pending_events.py
        - services/api/AGENT.md
      services_affected:
        - services/api
      out_of_scope:
        - services/api/treadmill_api/webhooks/persist.py
    validation:
      - kind: deterministic
        description: |
          The pending_events test suite passes against the refactored API.
        script: |
          cd services/api && uv run pytest tests/test_pending_events.py -q
      - kind: deterministic
        description: |
          pending_events.py exposes the new helper and the opaque-key signature.
        script: |
          grep -lE "pr_pending_buffer_key|pending_buffer_key" services/api/treadmill_api/webhooks/pending_events.py
      - kind: deterministic
        description: |
          Both ingress paths and both consumer drain sites derive
          the key via the helper.
        script: |
          grep -lE "pr_pending_buffer_key" services/api/treadmill_api/routers/webhooks.py
          grep -lE "pr_pending_buffer_key" services/api/treadmill_api/coordination/consumer.py
          grep -lE "pr_pending_buffer_key" services/api/treadmill_api/coordination/webhook_inbox.py
      - kind: deterministic
        description: |
          AGENT.md references ADR-0063.
        script: |
          grep -lE "ADR-0063" services/api/AGENT.md

  - id: persist-and-resolve-helper
    title: "ADR-0063 Step 3 — persist_and_resolve_webhook_event shared helper"
    workflow: wf-author
    depends_on: [task.generalize-pending-events-key.pr_merged]
    intent: |
      STUDY:
        - The post-Step-2 shape of `routers/webhooks.py` and
          `coordination/webhook_inbox.py`. Both now do the same
          three-step sequence: lookup task_prs → buffer-on-miss →
          Event INSERT. That sequence is what we centralize.

      BUILD: new module `services/api/treadmill_api/webhooks/persist.py`
      with `persist_and_resolve_webhook_event(session, normalized,
      body_json, redis_client, publisher) -> Event` that:
        - Performs the case-insensitive task_prs lookup for repo +
          pr_number (only when `normalized.repo` and
          `normalized.pr_number` are both present).
        - Persists the Event row with the resolved task_id (or NULL).
        - On miss with redis_client available, buffers via
          `buffer_pending_event(pr_pending_buffer_key(...), event.id)`.
        - Publishes via `publisher.publish(event, typed)`; on
          publish failure logs and returns successfully (the row
          is persisted; consumer rescan recovers).
        - Returns the persisted Event so callers can include
          `event_id` in their HTTP responses or log lines.

      Refactor both `routers/webhooks.py` and `coordination/
      webhook_inbox.py` to call the new helper. Their local code
      shrinks to: normalize → call helper → return / proceed.

      Update `services/api/AGENT.md`'s key-surfaces with an entry
      for the new module + a "Recent changes" bullet noting the
      lock-step requirement.
    scope:
      files:
        - services/api/treadmill_api/webhooks/persist.py
        - services/api/treadmill_api/webhooks/__init__.py
        - services/api/treadmill_api/routers/webhooks.py
        - services/api/treadmill_api/coordination/webhook_inbox.py
        - services/api/tests/test_webhook_persist.py
        - services/api/tests/test_integration_webhook_inbox.py
        - services/api/AGENT.md
      services_affected:
        - services/api
      out_of_scope:
        - services/api/treadmill_api/coordination/consumer.py
        - services/api/treadmill_api/webhooks/pending_events.py
    validation:
      - kind: deterministic
        description: |
          Tests covering the new helper and both ingress paths pass.
        script: |
          cd services/api && uv run pytest tests/test_webhook_persist.py tests/test_integration_webhook_inbox.py -q
      - kind: deterministic
        description: |
          The new module exposes the helper.
        script: |
          grep -lE "def persist_and_resolve_webhook_event" services/api/treadmill_api/webhooks/persist.py
      - kind: deterministic
        description: |
          Both ingress paths call the helper.
        script: |
          grep -lE "persist_and_resolve_webhook_event" services/api/treadmill_api/routers/webhooks.py
          grep -lE "persist_and_resolve_webhook_event" services/api/treadmill_api/coordination/webhook_inbox.py
      - kind: deterministic
        description: |
          AGENT.md references ADR-0063.
        script: |
          grep -lE "ADR-0063" services/api/AGENT.md

  - id: ingress-lockstep-ci-gate
    title: "ADR-0063 Step 4 — CI gate for webhook ingress lock-step"
    workflow: wf-author
    depends_on: [task.persist-and-resolve-helper.pr_merged]
    intent: |
      STUDY:
        - The post-Step-3 shape: every webhook ingress goes through
          `persist_and_resolve_webhook_event` for Event INSERT.
        - Existing non-webhook Event writers — `coordination/triggers.py`,
          `coordination/consumer.py`, `coordination/stuck_task_sweep.py`,
          and any sites that emit task / step / plan lifecycle events.
          These are legitimate writers and stay outside the gate.

      BUILD: new pytest file
      `services/api/tests/test_webhook_ingress_lockstep.py` that:
        - Scans `services/api/treadmill_api/routers/*.py` and
          `services/api/treadmill_api/coordination/*.py` for direct
          Event-row writes: `session.add(Event(`, `pg_insert(Event)`,
          `insert(Event)`, `session.merge(Event(`.
        - Allows the helper site (`webhooks/persist.py`) and the
          curated allowlist of existing non-webhook event writers.
          Allowlist is a hard-coded list in the test file, one entry
          per allowed path, each with a one-line comment naming the
          legitimate reason (e.g. "lifecycle event publisher").
        - Fails with a clear message naming the offending file +
          line(s) if it finds an Event write outside the allowlist.

      The test runs in the standard CI suite — no new infrastructure.
      Document the lock-step rule in `services/api/AGENT.md` with a
      pointer to ADR-0063.
    scope:
      files:
        - services/api/tests/test_webhook_ingress_lockstep.py
        - services/api/AGENT.md
      services_affected:
        - services/api
      out_of_scope:
        - services/api/treadmill_api/routers/
        - services/api/treadmill_api/coordination/
        - services/api/treadmill_api/webhooks/
    validation:
      - kind: deterministic
        description: |
          The new lock-step gate test passes against the current
          codebase (no offending writers outside the allowlist).
        script: |
          cd services/api && uv run pytest tests/test_webhook_ingress_lockstep.py -q
      - kind: deterministic
        description: |
          The gate file exists and contains the allowlist constant
          plus the scan logic.
        script: |
          grep -lE "ALLOWLIST|allowed_writers|lock.?step" services/api/tests/test_webhook_ingress_lockstep.py
      - kind: deterministic
        description: |
          AGENT.md documents the lock-step rule with ADR-0063.
        script: |
          grep -lE "lock.?step" services/api/AGENT.md
          grep -lE "ADR-0063" services/api/AGENT.md
```

## Diagram

Not applicable — ADR-0063 carries the canonical sequence diagram
for the buffer + drain flow. Plan readers should reference the ADR.

## Risks / unknowns

- **Merge conflicts across the chain.** Steps 2 and 3 both touch
  `routers/webhooks.py` and `coordination/webhook_inbox.py`.
  Mitigation: the `depends_on` chain serializes them; Step 3
  rebases on Step 2's main commit before the worker runs.
- **CI gate allowlist creep.** A future Event writer added without
  ADR justification would force the allowlist to grow. Mitigation:
  the allowlist is human-curated in the test file; any PR that
  adds an entry must reference an ADR explaining why the writer
  belongs there. We'll abort + amend ADR-0063 if the allowlist
  grows past ~5 entries without exception ADRs.
- **Existing NULL-`task_id` events on main don't backfill
  automatically.** Mitigation: out of scope; the back-fill path
  at `coordination/consumer.py:935` will drain them whenever the
  pr_merged event for an affected PR is processed.

## Decisions captured during execution

(empty at draft time; appended as work progresses)

## Post-mortem

(filled when plan transitions to completed)
