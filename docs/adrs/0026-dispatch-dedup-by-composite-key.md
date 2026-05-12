# ADR-0026: Dispatch dedup by composite key

- **Status:** proposed
- **Date:** 2026-05-12
- **Related:** ADR-0011 (immutable runtime), ADR-0017 (webhook ingestion), ADR-0021 (plan-merge trigger), ADR-0022 (per-kind dispatch); cribs RAMJAC's `PublishedMedication` dedup pattern

## Context

A single PR can fire `pull_request_review` and `pull_request_synchronize`
webhooks repeatedly:

- `pull_request_review` fires each time someone posts a review.
  Treadmill's wf-review posts comments → triggers wf-feedback. If
  wf-feedback subsequently produces a fix-commit, that fires
  `pull_request_synchronize`, which fires another wf-review run on
  the *same* PR head SHA. Without dedup, wf-review re-runs against
  identical content, posts a near-identical review, fires another
  `pull_request_review`, fires another wf-feedback, etc. Live
  observed today: PR #10 accumulated 3+ wf-review runs + 2+
  wf-feedback runs against overlapping commit SHAs. The chain
  doesn't naturally terminate; it just burns Claude credits.

- The `pull_request_synchronize` redelivery from SQS visibility
  expiry (ADR-0025 fixes that half) similarly causes duplicate
  wf-review runs. Even after the heartbeat lands, a PR's branch
  legitimately accruing multiple commits triggers multiple
  synchronizes; each could be a separate wf-review run if we don't
  dedup.

Captured as task #106. The fix is "don't dispatch a wf-review run
when one already exists for this exact `(repo, pr_number, head_sha)`."
Same logic for wf-feedback against `(repo, review_id)`.

RAMJAC's pattern (`PublishedMedication` table in
`/home/joe/ramjac/service/mar_medication_detector/src/model/published_medication.py`):

- Postgres table with a composite primary key.
- Optimistic pre-check before doing work.
- DB unique constraint as the source-of-truth gate.
- `IntegrityError` caught as expected behavior (concurrent race
  accepted).
- No broker-side dedup; the dedup lives where the dispatch decision
  lives.

Treadmill's analog: a `workflow_dispatch_dedup` table queried by the
trigger evaluator before each `dispatch_task` call.

## Decision

### New `workflow_dispatch_dedup` table

```sql
CREATE TABLE workflow_dispatch_dedup (
    dedup_key       text   NOT NULL,
    workflow_run_id uuid   NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    dispatched_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (dedup_key)
);
```

- `dedup_key` is a deterministic string built by the trigger
  evaluator from the event payload. Shape:
  `<workflow_id>:<repo>:<discriminator>`. Examples:
  - `wf-review:joeLepper/treadmill:pr=10,sha=b89360c1...`
  - `wf-feedback:joeLepper/treadmill:review=PRR_kwDOSb...`
  - `wf-ci-fix:joeLepper/treadmill:run_id=12345`
- `workflow_run_id` ties the dedup row to the run it gated; on
  cascade-delete (run cleanup), the dedup row drops too.
- Primary key is `dedup_key` alone (not composite-spread), which
  makes the duplicate-check a fast index lookup.

### Where the dedup key is built

The trigger evaluator (in `coordination/consumer.py` or its sibling
trigger-handler modules) gains a per-workflow `dedup_key_for(event)`
function:

```python
DEDUP_KEY_BUILDERS: dict[str, Callable[[Event], str | None]] = {
    "wf-review":   lambda e: f"wf-review:{e.repo}:pr={e.pr_number},sha={e.head_sha}",
    "wf-feedback": lambda e: f"wf-feedback:{e.repo}:review={e.review_id}",
    "wf-ci-fix":   lambda e: f"wf-ci-fix:{e.repo}:check_run={e.check_run_id}",
    "wf-conflict": lambda e: f"wf-conflict:{e.repo}:pr={e.pr_number},sha={e.head_sha}",
}
```

Workflows whose dedup key isn't deterministic (or which legitimately
allow N runs per trigger — e.g., wf-author has no natural dedup
because a single PR can produce many sequential code-author runs)
return `None`, signaling "dispatch every time."

### Pre-dispatch pattern (mirrors RAMJAC line-for-line)

```python
async def maybe_dispatch(...) -> WorkflowRun | None:
    dedup_key = DEDUP_KEY_BUILDERS.get(workflow_id, lambda _: None)(event)
    if dedup_key is None:
        # Workflow opts out; always dispatch.
        return await dispatch_task(...)

    # Optimistic pre-check.
    existing = await session.execute(
        select(WorkflowDispatchDedup).where(
            WorkflowDispatchDedup.dedup_key == dedup_key
        )
    )
    if existing.scalar_one_or_none() is not None:
        logger.info(
            "dispatch_dedup: skipping duplicate %s for %s",
            workflow_id, dedup_key,
        )
        return None

    # ... build the run + persist ...
    run = await dispatch_task(...)

    # Source-of-truth gate: PK constraint catches concurrent races.
    try:
        await session.execute(
            insert(WorkflowDispatchDedup).values(
                dedup_key=dedup_key, workflow_run_id=run.id,
            )
        )
        await session.commit()
    except IntegrityError:
        # Concurrent duplicate; another transaction already
        # dispatched a run for this dedup_key. Roll back the run
        # we just created (or accept it as a benign dup — the
        # workflow_run insertion is idempotent on its own ID).
        await session.rollback()
        logger.info(
            "dispatch_dedup: concurrent dispatch detected for %s; "
            "deferring to the winner",
            dedup_key,
        )
        return None

    return run
```

The duplicate `await dispatch_task(...)` + rollback isn't ideal
ordering — we could check first, then dispatch, but the PK
constraint is the source of truth. For v0 simplicity, the
pre-check covers the common case; the constraint catches the
narrow race.

A cleaner ordering: insert the dedup row *first*, catch
`IntegrityError` immediately, only then call `dispatch_task` if the
insert succeeded. This avoids ever creating a duplicate run. Will
adopt that ordering in the implementation.

### Discriminator parts (what goes in the dedup key)

| Workflow | Discriminator | Rationale |
|---|---|---|
| wf-review | `pr=<N>,sha=<head_sha>` | A review evaluates the diff at a specific head SHA. New SHA = new diff = new review. Same SHA = identical content = no new review. |
| wf-feedback | `review=<review_id>` | One feedback per review. A new review on the same SHA is a separate input. |
| wf-ci-fix | `check_run=<check_run_id>` | One fix attempt per failing check_run. |
| wf-conflict | `pr=<N>,sha=<base_sha>` | A conflict resolution depends on what main looks like; same base = same conflict. |
| wf-author | (opts out — None) | wf-author runs are dispatched per Task, gated by `task.<predecessor>.pr_merged` (or unconditionally); dedup is task-level via the existing `tasks` table PK. |
| wf-plan | (opts out — None) | wf-plan dispatches from `plan_doc_merged` events; the ADR-0021 handler already dedupes by `plan_id = uuid5(repo:path@sha)`. |
| wf-validate | TBD per Ralph-loop ADR | The Ralph-loop verdict is gated; dedup probably `pr=<N>,sha=<head_sha>,attempt=<n>` to support a retry budget. |

### Optimistic pre-check + PK gate ordering

Per RAMJAC's discipline:

1. **Build the dedup_key.**
2. **Try to insert dedup row first** (catch `IntegrityError` → another transaction wins → skip).
3. **If insert succeeded**, proceed to build the workflow_run.
4. **Commit both** in the same transaction.

Order matters: insert-first means we never create a workflow_run row
that we then have to roll back. The runtime impact: the dedup row
exists momentarily without its run row; the FK to `workflow_runs(id)`
is added in the second INSERT. To make this work cleanly, the
`workflow_run_id` column is `NULLABLE` initially; a follow-up UPDATE
sets it after the run exists. OR: defer the FK to a deferrable
constraint; OR: relax the FK entirely (the dedup table is purely a
gate, not a relationship lookup). v0 picks the relaxed-FK option:
drop the FK, keep `workflow_run_id` as a `text` reference (or `uuid`
without FK).

### What this does NOT do

- Doesn't dedup *executions* (workers retrying the same step).
  ADR-0025's heartbeat + don't-delete-on-error handles that case.
- Doesn't dedup at the SQS layer (FIFO dedup-ids). The dedup decision
  is application-level: "should this workflow_run exist at all?"
- Doesn't backfill dedup rows for runs that already exist. New runs
  going forward; old runs are grandfathered.
- Doesn't expose a "force re-dispatch" mechanism. The operator who
  wants to re-review a PR they've already reviewed can either: (a)
  push a fresh commit (new SHA → new dedup key); (b) delete the
  dedup row manually; (c) wait for a future "force re-review" CLI
  flag. v0 doesn't ship (c).

## Bunkhouse precedent

Bunkhouse doesn't have this problem because its trigger machinery
isn't webhook-driven in the same way — bunkhouse's "next thing to
do" is determined by the orchestrator process, not by GitHub
webhook fan-out. So bunkhouse has no `wf-review` dedup analog.
Treadmill's webhook-driven trigger evaluator is novel; this is a
new pattern.

## Trade-offs

- **One new table.** Tiny. PK on a text column; sub-microsecond
  lookup. No indexes beyond the PK.
- **One extra INSERT per dispatched run.** Inside the same
  transaction as the workflow_run insert; trivial.
- **The dedup logic lives in the consumer.** Today's trigger
  evaluator (which decides what to dispatch from event_triggers
  matches) gets a pre-dispatch hook. Small refactor.
- **Failure mode**: if a Postgres outage happens between
  "dedup_row inserted" and "workflow_run inserted," the dedup row
  is orphaned (no workflow_run with that ID will ever exist). The
  next attempt to dispatch the same dedup_key will hit the orphan +
  skip. That's a poison case requiring operator cleanup. Mitigation:
  insert both in a single transaction with proper rollback
  semantics. Postgres handles this natively.
- **Operator visibility**: the dedup row is a queryable artifact —
  `SELECT * FROM workflow_dispatch_dedup WHERE dedup_key LIKE 'wf-review:%' ORDER BY dispatched_at DESC` gives the operator a clear "what did we dispatch, when, and which run was it?" view.

## Alternatives considered

- **Use SQS FIFO dedup IDs.** Built-in 5-minute dedup window. Too
  short for our case (a PR can have intermediate commits hours apart
  that legitimately produce new reviews). Rejected.
- **Track dedup state in Redis.** Faster lookups. Adds Redis as a
  load-bearing component for dispatch correctness. Postgres is
  already load-bearing for the events table; one source of truth is
  simpler. Rejected unless lookup latency becomes notable.
- **Query workflow_runs + events tables for existing runs instead
  of a dedicated table.** Possible but slower + the predicate is
  complex (join workflow_runs to events filtered by payload.head_sha).
  Dedup table is a denormalized index for the same answer. Rejected
  vs the table.
- **Dedup at the webhook ingestion layer** (before SQS enqueue). The
  webhook poller would check whether this `(pr_number, head_sha,
  event_type)` was already seen; if yes, skip the SQS enqueue.
  Possible but pushes dedup state earlier than needed; the trigger
  evaluator is the natural seam (it knows which workflow is being
  dispatched, which is what the dedup key needs). Rejected at v0;
  could revisit if dedup at the poller layer becomes important.
- **Don't dedup; rely on the operator manually closing/aborting
  duplicate runs.** What we have today. Rejected per task #106.

## Open questions

- **Q26.a — How does this compose with the Ralph-loop validation
  ADR (forthcoming)?** Validation likely wants its own dedup
  semantics: "have we already validated this `(pr_number, head_sha)`,
  and what was the verdict?" Ralph-loop ADR will define its own
  dedup discriminator; same table, different `dedup_key` prefix.
- **Q26.b — Cleanup / TTL.** Dedup rows accumulate forever. At
  single-operator personal scale, that's fine (a few hundred rows
  total over a year). At higher volumes, a periodic cleanup job
  trimming rows older than N days might matter. Defer.
- **Q26.c — Cross-deployment dedup leak.** A PR # is per-repo, per-
  deployment. The dedup_key includes the repo, so personal-Treadmill
  and employer-Treadmill don't collide. Good.
- **Q26.d — Manual override.** What if the operator genuinely wants
  to re-review a PR for QA purposes? The clean answer is "push a
  trivial commit to flip the SHA"; the less-clean alternative is a
  CLI command to delete the dedup row. Defer until requested.
- **Q26.e — Should the existing five seeded `event_triggers` rows
  start carrying dedup discriminators in their config?** Currently
  they're flat (`event_type → workflow_id`). The dedup key builder
  lives at the consumer code level today; future evolution could
  push it into the `event_triggers` table as a column. Defer
  unless the operator wants to configure dedup per row.

## Consequences

- **DB migration**: add the `workflow_dispatch_dedup` table.
  Alembic upgrade picks it up on next API startup.
- **`services/api/treadmill_api/coordination/consumer.py`** (or its
  trigger-evaluator sibling): gains a `dedup_key_for_dispatch(event,
  workflow_id)` function + an insert-first/dispatch-second
  wrapper.
- **Constants module** (probably
  `services/api/treadmill_api/dispatch.py` or a new
  `dispatch_dedup.py`): the `DEDUP_KEY_BUILDERS` dict.
- **Tests**: unit tests for each builder (handful of synthetic
  events → expected dedup keys); integration test where the same
  event lands twice and the second dispatch is skipped.
- **Composes with ADR-0025** (heartbeat): together they close the
  duplicate-runs failure mode. Heartbeat prevents accidental
  redelivery from SQS; dedup prevents legitimate-but-redundant
  dispatches from event fan-out.
- **Composes with the Ralph-loop ADR** (forthcoming): Ralph-loop's
  retry budget will use a `dedup_key` that includes an `attempt`
  counter (same table, different discriminator).
- **Phase 2 self-driving criterion**: a PR can accrue arbitrary
  pr_review / pr_synchronize webhook traffic without producing
  duplicate wf-review or wf-feedback runs.
