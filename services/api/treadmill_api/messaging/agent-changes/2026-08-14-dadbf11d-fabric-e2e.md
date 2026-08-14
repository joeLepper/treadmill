- **[#PR] Fabric e2e — cross-host + isolation + broadcast definition of done (task dadbf11d, ADR-0093/0094)**:
  adds `test_fabric_e2e.py` (5 sandbox tests + 1 integration-gated) and extends
  `host_consumer.py` with a second isolation enforcement wall.

  **`host_consumer.py` change — handle() defense-in-depth:**
  `HostConsumer` now stores `_local_labels: frozenset[str]` (updated at the
  start of each `run_once()` call from `labels_on_host`). `handle()` checks the
  message's `ordering_key` against `_local_labels` and REFUSES the relay if the
  label is not in the local set — log warning + return without calling `relay_fn`.
  This is the SECOND enforcement: even if `read_for_labels` regresses and leaks a
  cross-host row past the first wall (the SQL filter), `handle()` still refuses.
  The claim still commits so the leaked row is not redelivered to this consumer;
  the correct host's consumer has its own `consumer_id` and dedup slot.

  **Req A — `read_for_labels` integration test (TREADMILL_INTEGRATION-gated):**
  `test_read_for_labels_integration` appends two rows for `label_a` (host-a) and
  one for `label_b` (host-b) against a real Postgres instance; asserts
  `read_for_labels([label_a])` returns exactly the two label_a rows, all with
  `ordering_key == label_a`, ascending offsets — no cross-host bleed through the
  SQL `IN` filter. Rolls back after. Skipped in sandbox.

  **Req B — handle() planted-leak unit test (RED-then-GREEN):**
  `_NaiveHostConsumer` subclass overrides `handle()` without the defense.
  `_LeakyLogStore` bypasses the read_for_labels filter (returns all rows
  regardless of label). RED: naive consumer relays `agent-b`'s row when running
  on host-a (proves non-vacuity). GREEN: real `HostConsumer` refuses `agent-b`,
  still relays `agent-a`; decoded payload asserted.

  **Req C — three drain-level sandbox scenarios (RED-then-GREEN, decode actual):**
  `_SharedSQLiteLog` (SQLite WAL, AUTOINCREMENT offset, mirrors EventLogStore
  interface) serves as the shared central log for both "host" instances.
  `_LogPublisher` bridges `OutboxPump.RowPublisher` → `_SharedSQLiteLog.append`.
  `_NoopPublisher` simulates a cut link (publish is a no-op).
  `_RelayCapture` captures relay deliveries for assertion (no filesystem writes).

  1. **CROSS-HOST**: writes two messages addressed to `agent-b` into host-a's
     outbox. RED: `_NoopPublisher` → pump runs but writes nothing → host-b
     receives 0 messages → assertion that both messages arrived fails. GREEN:
     `_LogPublisher` → pump drains 2 rows → host-b consumer delivers both →
     payloads decoded and asserted `seq=[1,2]` in order. DUP-DROPPED: cursor
     reset, second run → dedup blocks re-delivery; still exactly 2.

  2. **ISOLATION**: host-b writes N=4 events to its local outbox while the pump
     is not run (link cut). RED: log has 0 rows → assertion that all N arrived
     fails. No-adoption check: `host_store.labels_on_host("host-b")` returns the
     same bindings — nothing adopted host-b's labels. GREEN: pump runs (link
     restored) → drains all 4 → host-b consumer delivers all 4 decoded in order
     (`seq=[1,2,3,4]`). Exactly-once: second consumer run delivers nothing new.

  3. **BROADCAST**: both `agent-a` and `agent-b` join `team-channel`. RED:
     fanout not called → 0 events → both consumers receive nothing. GREEN:
     `fanout()` emits one unicast row per subscriber into `_SharedSQLiteLog`
     with composite `dedupKey = f"{subscriber}:{broadcast_id}"`. host-a's
     consumer decodes its copy (`label=agent-a`, correct payload); host-b's
     consumer decodes its copy (`label=agent-b`, correct payload). Distinct
     dedupKeys asserted. Exactly-once: second run delivers nothing new to either.
