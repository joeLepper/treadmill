# ADR-0089: Token-economics controls — wake filtering, a wired meter, cache-aware cadence

- **Status:** proposed
- **Date:** 2026-06-11
- **Related:** ADR-0068 (treadmill-events channel), ADR-0071 (per-session
  relay verbosity), ADR-0087 (long-lived team execution model)

## Context

We measured a full day of the live team loop (2026-06-10: 64 PRs merged
unattended) by harvesting per-call usage from session transcripts, because
the purpose-built meter — the `llm_calls` table — has zero rows: we built
the schema and never wired a writer. The day's totals across 8 sessions:

| metric | value |
|---|---|
| LLM calls | 13,412 |
| output tokens | 11.0M |
| fresh input tokens | 1.7M |
| cache-creation tokens | 62.3M |
| **cache-read tokens** | **7,494M** |
| cache hit rate | 99% (96% evaluator) |

Four findings drive this decision:

1. **Cache reads dominate the bill.** At API-equivalent prices the day is
   ~85% cache reads — even at their 10× discount, 7.5B read tokens dwarf
   everything else. The hit rate is already excellent, so the lever is not
   caching better; it is **context size × call count**.
2. **Call count is inflated by noise wakes.** Long-running orchestrator
   sessions are woken by every `github.check_run_completed` and
   `github.pr_synchronize` their watched tasks emit — hundreds of wakes per
   day whose entire useful output is "quiet," each re-reading a ~500k-token
   context from cache. These per-check events are load-bearing for the
   coordinator's bookkeeping, but for orchestrators they carry no decision
   in ≥95% of firings (terminal events — merges, verdicts, escalations —
   carry the decisions).
3. **The tier differential compounds both findings.** Orchestrator
   sessions run a premium model tier; the worker cluster runs a cheaper
   tier (operator decision, 2026-06-11). Every noise wake and every
   implementation task held at the orchestrator layer is paid at the
   premium rate — wake filtering and dispatch-to-workers are the same
   economic move at two different layers.
4. **We cannot manage what we re-derive by hand.** The transcript harvest
   that produced the table above took an evening and is already stale; the
   meter must be standing infrastructure or economics will never inform
   routine decisions.

A fourth observation shapes the cadence rule: the evaluator's 96% hit rate
(vs 99–100% elsewhere) is the cache-expiry coupling made visible — its
slow, bursty cadence pays cold-read tax. Wake cadence interacts with the
~5-minute prompt-cache TTL.

## Decision

### 1. Wake-class filtering at the channel server (the big lever)

`treadmill-events.ts` gains a per-session **wake filter**: an event-class
allowlist deciding which events become `notifications/claude/channel`
wakes at all. Same knob family as ADR-0071's relay levels (which govern
Telegram verbosity) — this governs whether the session wakes.

- Config: `TREADMILL_WAKE_ACTIONS` (comma-separated `entity.action`
  globs) with **role-based defaults**:
  - `orchestrator` default: `github.pr_merged`, `task.*_verdict`,
    `task.escalat*`, `task.registered`, `task.cancelled`,
    `prod_promotion.*`, `deploy.failed`, `staging_smoke.failed`,
    `datamigration.*` (ADR-0092's first-success validation gates are
    alerted-class by design), relay messages (always), reconcile frames
    (always).
  - `coordinator` / `evaluator` / `worker` default: unfiltered (their
    bookkeeping consumes the noisy classes today).
- **Layering invariant (wake ⊇ relay):** the ADR-0071 relay level
  selects from events that already woke the session, so a session's wake
  filter must be a superset of its relay set — a relay-significant event
  that never wakes can never relay. The channel server WARNs at startup
  when the configured pair violates the superset, keeping the two knobs
  one layered family rather than two drifting ones.
- Suppressed events are not dropped: the server keeps a per-session
  **digest counter** and prepends a one-line summary to the next delivered
  wake (`suppressed since last wake: 47 check_run_completed, 3
  pr_synchronize across 2 tasks`) — state remains reconcilable, and a
  session can always pull ground truth from the API.
- Estimated effect: thousands of orchestrator wakes/day removed; at the
  measured ~500k context per wake this is the largest single line item in
  the cache-read bill.

### 2. Wire the meter: an `llm_calls` harvester + standing report

A harvester (CLI + cron) tails session transcript JSONL, extracts per-call
usage (input/output/cache-creation/cache-read, model, timestamp), maps
session → label → `task_executions` window where one matches, and inserts
into `llm_calls`. Orchestrator/coordinator calls get a synthetic
per-session execution row (or the column relaxes to nullable — implementer
decides, documents the choice). A `treadmill tokens report` command renders
the daily rollup (per label, per task, cache-hit ratio) so token economics
is a standing input to orchestrator decisions, not an evening of forensics.

### 3. Cache-aware cadence as documented convention

The wakeup discipline some sessions already practice ad hoc becomes a
written convention in the session templates: when actively watching
fast-changing state, poll inside the cache window (≤270s); otherwise
commit to long intervals (≥20 min) and accept one cold read — never the
worst-of-both ~5-minute middle. Bursty-but-rare roles (evaluator) batch
work when woken rather than waking often.

## Consequences

- Orchestrator sessions stop paying half-megatoken reads to say "quiet";
  the events table remains the complete record (filtering is at the wake
  edge, not persistence).
- The digest line preserves situational awareness; a filtered session that
  needs per-check granularity for a specific intervention can temporarily
  widen its own filter (env restart) or poll the API directly.
- Watcher patterns shift from "wake on every event, mostly ignore" to
  "wake on decisions, poll deliberately when intervening" — consistent
  with how interventions actually ran on 2026-06-10.
- The meter creates a feedback loop: the wake filter's effect is verified
  by the report it enables (call count + cache-read deltas, before/after).
- Risk: an over-tight filter delays noticing a stall. Mitigated by the
  digest line, the stall-detection heartbeats (which poll the DB, not the
  wake stream), and coordinator escalations remaining always-wake.

## Out of scope

- Model routing / downgrading by task class.
- Automated context compaction or forced session restarts.
- Fleet sizing (worker-3 idled at 8 calls on 2026-06-10 — a dispatch
  observation for the coordinator, not a cost control).
- Changing what the coordinator/evaluator/workers receive (their defaults
  stay unfiltered until their own consumption is measured).
