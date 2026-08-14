# treadmill_api/messaging

## Purpose

This package implements the SQLite single-pump variant of the durable
outbox and idempotent dedup consumer described in ADR-0093 and ADR-0094.
It provides crash-safe, effectively-once message delivery for inter-agent
communication when the host is isolated from the network. All storage uses
SQLite (WAL mode) so the component works without any external service.

## Key surfaces

- **`outbox.py`** — `OutboxBackend`. Append-only SQLite outbox with three
  methods: `write(message)`, `read_pending(limit)`, `mark_published(row_id)`.
  The drain loop calls `read_pending` and `mark_published`; writers call
  `write`. Each row carries `dedupKey`, `ordering_key`, `event_type`,
  `payload`, `created_at`, and `published_at` (NULL until drained).
- **`dedup.py`** — `DedupBackend` + `ClaimResult`. Implements the
  claim→commit cycle from ADR-0093 (provenance: ramjac ADR-0014).
  Dedup is scoped to `(dedup_key, consumer_id)` so two consumers processing
  the same event each receive one delivery. A claimed key whose `expires_at`
  has passed is re-claimable (the previous claimant died mid-handler).
- **`consumer.py`** — `MessageConsumer`. Base class. Subclass and override
  `handle()`. The `deliver(message)` method runs the full cycle: claim →
  handle → commit. If `handle()` raises, the claim is NOT committed; the
  crash window expires and the message redelivers.
- **`pump.py`** — `OutboxPump`. Drains pending outbox rows to the event log
  in per-recipient append order. Acquires `LOCK_EX | LOCK_NB` on a sidecar
  lock file at startup; a second pump on the same lock file is refused. The
  flock is held for the pump's lifetime and released automatically on process
  death — no stale pidfile risk. Retries publish indefinitely; never drops.
- **`broadcast.py`** — `ChannelStore` + `fanout()`. Channel subscription as
  a durable fact (join/leave are `SubscriptionEvent` records with offsets);
  `subscribers_at(channel, up_to_offset)` folds those events to compute the
  subscriber set at any offset. `fanout()` expands a broadcast to N unicast
  deliveries honoring three ADR-0093 rules (see below). emit() is injected
  so callers use any log backend (outbox, EventLogStore, test spy).

## Broadcast — channel subscription + N-unicast delivery (ADR-0093)

Three load-bearing rules (ADR-0093 §Broadcast, provenance: ramjac ADR-0009):

1. **Subscriber set = log fold at the broadcast's offset.** Deterministic and
   replayable. A join after that offset misses the broadcast; a leave after it
   does not un-deliver. Use `ChannelStore.subscribers_at(channel, up_to_offset)`.
2. **dedupKey = composite `f"{subscriber}:{broadcast_id}"`** — never a fresh UUID
   (fresh UUID lets re-fanout reprocess) and never `broadcast_id` alone (shared
   key + shared dedup blocks all but the first subscriber).
3. **Order is per-subscriber** (ordering_key = subscriber label). Two subscribers
   may see the same broadcast at different positions in their own mail.

`fanout()` calls `emit(subscriber, dedup_key, payload)` for each subscriber.
In production, emit appends a unicast event to the central log (EventLogStore);
HostConsumer delivers it via cc-relay on that host. Broadcast is N unicast
deliveries — no new delivery mechanism is introduced.

## Invariant foils (ported from ramjac-events, task f6db308b)

Two load-bearing red-then-green foils in `test_messaging_invariants.py`.
Neither is derivable from ADR prose — re-deriving produced the mark-then-process
bug donna found in review (2026-08-12).

- **INVARIANT-1 — silent-crash / mark-then-process.**
  `_MarkThenProcessCrashingConsumer` commits the `dedupKey` BEFORE the handler
  runs. A mid-handler crash leaves the key permanently committed; redelivery
  sees DUPLICATE and drops the message — the handler never runs. This is the bug
  ADR-0093 Decision §3 ("never mark-then-process") exists to prevent.
  RED: handler runs 0 times on broken variant.
  GREEN: correct `MessageConsumer` redelivers; handler runs exactly once.

- **INVARIANT-2 — unscoped-dedup-collision.**
  `_UnscopedDedupConsumer` uses a shared `consumer_id` for all consumers.
  After subscriber-A commits a `dedupKey`, subscriber-B's delivery is seen as
  DUPLICATE and dropped — each subscriber does NOT get its own delivery slot.
  This violates ADR-0093 per-subscriber delivery and ramjac ADR-0014 §3
  (dedup on `event_id × subscriber_id`, never `event_id` alone).
  RED: subscriber-B receives 0 messages on broken variant.
  GREEN: correct per-`consumer_id` scoping gives each subscriber exactly one delivery.

Provenance: ramjac-events invariant test suite, ported 2026-08-13 (task f6db308b).
Each ported unit carries a `# Provenance: ramjac-events …` comment so a later
ramjac fix can be re-ported without re-deriving semantics.

## Recent changes

> **New entries are PER-PR FRAGMENT FILES, not prepends** (task 8736482f):
> add `agent-changes/YYYY-MM-DD-<task-or-pr-slug>.md` beside this AGENT.md —
> one entry per file, newest by filename; format in `docs/agent-md-schema.md`.
> Prepending here is the conflict factory that stacks same-day rework cascades
> (every in-flight PR inserts at this same anchor).

## Pitfalls

- **Single flock pump invariant.** Exactly one active pump per outbox at all
  times, enforced by `flock` on a sidecar lock file. ADR-0093 dedup makes a
  stray second pump non-corrupting (duplicates are dropped at consumers), but
  ordering is lost. Never bypass the flock by using different lock files for
  two pumps draining the same outbox in production.
- **Single consumer per consumer_id invariant.** `DedupBackend.claim()` is
  SELECT-then-INSERT/UPDATE and is safe only when exactly one consumer per
  `consumer_id` runs concurrently. The flock ensures this for the pump. Do
  NOT introduce multiple live consumers sharing a `consumer_id`.
- **Handlers MUST be idempotent.** A handler that succeeds but whose process
  dies before `commit()` will be redelivered and run again. The consumer
  provides effectively-once delivery ONLY when handlers are idempotent. Do
  not claim exactly-once processing.
- **crash_window=timedelta(0) in tests** makes claims expire immediately
  after creation. This is intentional for crash-safety tests; never use
  it in production — it makes every message re-claimable by the next
  delivery regardless of whether the prior handler finished.
- **Dedup is per (dedup_key, consumer_id).** Two consumers sharing a DB
  path but with different `consumer_id` values will each process the same
  key independently. This is the intended behavior for fan-out.
- **WAL mode + busy_timeout.** The DB is opened in WAL mode with a 5s
  busy timeout. Concurrent readers and one writer co-exist safely. Do not
  open the same DB path with `check_same_thread=False` from multiple
  threads without a connection-per-thread discipline.

## Navigation

- **ADR-0093** — durable ordered effectively-once agent messaging (the
  message contract and Property 3: claim→process→commit).
- **ADR-0094** — hosts survive isolation via local outbox (the outbox
  shape and SQLite backend rationale).
- **ramjac ADR-0014** — upstream provenance for the claim/commit cycle.
- **Adjacent:** `treadmill_api/eventbus.py` (the existing SNS publisher
  Protocol; Task 2 of plan 001cb672 wires the outbox to it).
- **Tests:** `services/api/tests/test_messaging_dedup.py`,
  `test_messaging_crash_safety.py`, `test_messaging_pump.py`,
  `test_messaging_invariants.py` (ported ramjac-events invariant foils — see below),
  and `test_outbox_pump_complete.py` (slice-1a completing semantics — flock refusal,
  write-then-crash redelivery foil, drain-on-reconnect in order; task 4ba55a27),
  and `test_messaging_broadcast.py` (broadcast channels — ONCE-PER-SUBSCRIBER with
  two foils, ISOLATED-AT-BROADCAST, JOIN-AFTER, LEAVE-AFTER; task dc40b385),
  and `test_fabric_e2e.py` (definition of done — cross-host/isolation/broadcast
  end-to-end: Req A real-Postgres read_for_labels isolation, Req B handle()
  defense-in-depth planted-leak foil, Req C three drain-level scenarios each
  RED-then-GREEN with decoded output; task dadbf11d).
