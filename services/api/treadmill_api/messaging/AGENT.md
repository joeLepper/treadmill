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

## Recent changes

> **New entries are PER-PR FRAGMENT FILES, not prepends** (task 8736482f):
> add `agent-changes/YYYY-MM-DD-<task-or-pr-slug>.md` beside this AGENT.md —
> one entry per file, newest by filename; format in `docs/agent-md-schema.md`.
> Prepending here is the conflict factory that stacks same-day rework cascades
> (every in-flight PR inserts at this same anchor).

## Pitfalls

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
- **Tests:** `services/api/tests/test_messaging_dedup.py` and
  `test_messaging_crash_safety.py`.
