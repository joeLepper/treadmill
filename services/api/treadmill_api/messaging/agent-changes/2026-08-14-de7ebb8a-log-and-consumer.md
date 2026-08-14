- **[#PR] Central event log + per-host consumer (task de7ebb8a, ADR-0093)**:
  adds the Postgres-backed central event log (`models/event_log.py` — `EventLog`
  ORM model with BIGSERIAL `offset` PK, `ordering_key`, `dedup_key`, `event_type`,
  `payload` JSONB, `created_at`) and its async accessor (`event_log_store.py` —
  `EventLogStore.append` + `EventLogStore.read_for_labels`). Alembic migration
  `20260814_0100_event_log.py` creates the table and an index on `ordering_key`
  to support the per-consumer `WHERE ordering_key IN (...) AND offset > cursor`
  query. Adds `messaging/host_consumer.py` — `HostConsumer` subclasses
  `MessageConsumer`; `run_once(session)` calls `labels_on_host(this_host)` first,
  reads the log for those labels only (ISOLATION INVARIANT: never fetches or
  relays labels bound to another host), and delivers each message via the
  claim→handle→commit cycle. `handle()` writes a relay file in
  `~/.cc-channels/<label>/relay/` (intra-host cc-relay wake only). `relay_fn` is
  injectable for tests. Tests: 10 tests in `test_messaging_host_consumer.py` —
  HOST-SCOPED (own labels only, empty-bindings, multiple labels), EFFECTIVELY-ONCE
  (same dedupKey blocked on second deliver, distinct keys both handled),
  PER-RECIPIENT ORDER (ascending offset, cursor advances, second poll does not
  re-deliver), ISOLATION FOIL red-then-green (red: `_naive_consume` relays
  cross-host labels; green: `HostConsumer.run_once` does not). Also updates
  `test_models.py` JSONB allowlist to add `event_log.payload` (ADR-0093
  schema-free payload; each event_type owns its shape).
