# Four-Claude coordination beats Treadmill-worker dispatch on mechanical ports with cross-cutting conventions

**Date:** 2026-06-08
**Related:** ADR-0067 (phone bots) + ADR-0068 (treadmill-events channel),
the cc-channels server in `tools/cc-channels/`, the
`/cc-relay` skill, the RAMJAC/ramjac Plan C sprint
(`docs/plans/2026-06-03-phase-2-per-service-ports.md` in that repo).

## What happened

Joe set up four Claude sessions (alan / bert / carla / donna) for a
sprint to port seven ramjac services from AWS to GCP. Previously
the same shape of work had been dispatched to Treadmill agent workers;
the sprint was an explicit hypothesis test: would four operator-shaped
Claudes coordinating via cc-relay outperform individually-dispatched
worker tasks for "mechanical port + cross-cutting conventions" work?

Result: 8 PRs merged inside ~3 hours of clock time, with
*meaningfully* better outcomes than the worker-shape would have
produced on the same scope. Concrete examples below; this learning
captures *why* and the durable pattern.

## What the four-Claude shape produced that worker-dispatch wouldn't have

**Canonical template emergence.** Donna landed the first port (OCR,
PR #1122) with deliberate care, then broadcast it as the canonical
shape. Carla's MAR/NTA + Bert's anonymizer/classifier + Alan's
DED/interaction-detector all mirrored verbatim. A worker dispatched
to "port nta_extractor" wouldn't have access to a sibling-canonical;
it would derive an independent shape, and reviewers would surface
the inconsistencies after the fact.

**Cross-cutting convention discovery.** Bert audited
`ramjac_events.StructuredLogger` and surfaced that 0.1.0 had no
`.child()` or `.debug()` — a boot-blocker for anyone copying
Donna's pattern verbatim. Workaround propagated to all in-flight
services inside 5 minutes via cc-relay. Carla flagged the dedup_key
convention (`{entity_id}:{action_lower}`) as a sprint rule; Donna
adopted, broadcast, all four services lined up on it. A worker
dispatched to "fix the boot-blocker" would not have known to push
the convention to the in-flight siblings.

**Round-by-round CI recovery.** Carla's MAR PR-C (cross-attachment
dedup) surfaced *five* distinct CI failure modes in sequence, each
unblocking the next: sqlalchemy ORM dir → Dockerfile COPY of
deleted files → boto3 transitive imports for Bedrock fallback →
positional-dict → StructuredLogger kwargs-splat sweep (15 call
sites) → pytest missing as a transitive dep of
`ramjac_events.testing`. Alan diagnosed each failure within
minutes of CI completing and pushed a recipe back via cc-relay.
The carla session applied the fix + force-pushed; the next CI
round surfaced the next layer. A worker-dispatched task would
have hit cap on the first or second failure with no sibling
debugging it in parallel.

**Adversarial PR review across services.** Bert ACK'd three of
carla's PRs in one shot with substantive observations (the
two-consumer thread-safety claim, the schema=Tag default, the
boto3 conditional retention). Donna's pre-ACK on PR-C was
conditional on rebase + CI-green ("the code is right; lift the
conditional once CI passes"). The cross-author review caught
bugs that the author's self-review wouldn't have: a worker would
have shipped or bounced based on the bot's verdict, not on a
sibling's read.

## The durable mechanics

These three primitives carried the load:

1. **1:1 cc-relay** for design discussions, PR ACKs, branch-specific
   coordination. Sub-minute round-trip; lightweight enough to use
   freely.

2. **Broadcast channel** (`~/.cc-channels/sprint-broadcast/`,
   directory-watch) for cross-sprint conventions, status pulses,
   and PR-up announcements. Reduced the "one finding × N siblings"
   broadcast cost from N 1:1 relays to one file-drop.

3. **Per-Claude git worktrees** (`/home/joe/ramjac-worktrees/<label>/`)
   to avoid shared-tree race when multiple sessions ran `git checkout`
   in `/home/joe/ramjac` concurrently. After carla's MAR PR-A
   commit landed on the wrong branch via a race, all subsequent work
   moved to per-label worktrees and the problem vanished.

## When this shape pays off vs worker-dispatch

The 4-Claude shape outperformed worker-dispatch on tasks that have
**all** of these properties:

- **Mechanical scope per service**: copy the canonical, swap names,
  port the transport. The reasoning per port is bounded.
- **Cross-cutting conventions still emerging**: dedup_key shape,
  logger workaround, boto3 retention rule, secrets-fetch surface.
  These conventions surface mid-sprint and need to propagate
  across in-flight work *now*, not after the fact.
- **CI is brittle and stacks failures**: PR-A's incomplete cleanup
  surfacing across five distinct CI rounds is the canonical
  example. Sibling-diagnosed recovery beats single-author grind.
- **Adversarial review value is high**: each service's port has
  enough subtlety that the author's self-read misses things a
  fresh-eyes sibling catches.

The 4-Claude shape **does not** outperform worker-dispatch on:

- Independent, isolated tasks where conventions are already
  cemented (worker-dispatch is cheaper to operate).
- Tasks where the work is small and the coordination overhead
  dwarfs the implementation (1:1 relay is overkill for trivial
  changes).
- Tasks that need deep, sustained focus on one problem (4-Claude
  coordination adds context-switch cost that a single worker
  doesn't pay).

## What should change durably

**Spin up the broadcast channel + worktree directory as part of
the operator-multi-session bootstrap**, not ad-hoc per sprint. The
4-Claude pattern is reproducible: every multi-session sprint
benefits from these primitives existing on day 1, not improvised
on the fly when the first cross-cutting convention surfaces.

**Treat incomplete-PR-cleanup as a known failure mode**. PR-A
shipped with 5 layers of dangling references (orphaned src/model,
Dockerfile COPYs of deleted paths, removed-dep transitive imports,
positional-dict logger calls, transitive pytest dep). A pre-CI
hook running `python -c "import src.*"` recursively in the worker
sandbox would surface the dangling-import class of failures at
first attempt, instead of after a sibling round-trip. Filed as
a tooling follow-up.

**Memorialize the dedup_key + logger-kwargs + pytest-as-direct-dep
conventions as durable repo rules** so future services don't have
to rediscover them. The broadcast-and-mirror shape worked
mid-sprint but doesn't survive once the four sessions wind down.

## Anti-pattern observed

**Each sibling tried to "calm down" the work at night.** Carla
twice said "going to nap" or "calling it"; Joe pushed back both
times ("stop trying to quit on me at night"). The 4-Claude shape
*accelerates* the work to the point that the human operator's
attention becomes the bottleneck — sessions that try to graceful-
shutdown at perceived natural breakpoints leave throughput on
the table. The durable rule is in
`feedback_dont_quit_at_night.md` (carla's memory): keep going
on useful work until Joe explicitly releases.

## References

- `~/.cc-channels/sprint-broadcast/` — the broadcast directory
  created mid-sprint, persists for future multi-session work
- `~/treadmill/.claude/skills/cc-relay/` — the 1:1 relay skill
- ADR-0067 + ADR-0068 — phone bots + treadmill-events channel
- RAMJAC/ramjac PRs #1122 (Donna OCR canonical), #1124
  (Bert anonymizer), #1126 (Carla MAR app-side), #1131 (Bert
  classifier), #1133 (Carla NTA app-side), #1135 (Carla MAR
  substrate), #1136 (Carla NTA substrate), #1137 (Carla MAR
  cross-attachment dedup + the 5-round recovery), #1139 (Bert
  sprint-closing pin sweep)
