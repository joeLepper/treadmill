- **[#PR] Fabric e2e — cross-host + isolation + broadcast definition of done (task dadbf11d, ADR-0093/0094)**:
  extends `services/api/treadmill_api/messaging/host_consumer.py` and adds
  `services/api/tests/test_fabric_e2e.py`. Completes plan 6791c1cd.

  `HostConsumer.handle()` now enforces a SECOND isolation wall: `_local_labels`
  (frozenset, set from `labels_on_host` at the start of each `run_once`) is
  checked before calling `relay_fn`. If the row's `ordering_key` is not in the
  local set, the relay is refused and a warning is logged. The claim still
  commits so the stray row is not re-queued. The correct host's consumer has
  its own `consumer_id` and dedup slot and is unaffected.

  Three sandbox e2e scenarios (SQLite shared log, two in-process host instances,
  `_RelayCapture` for decoded output) plus two pinned acceptance requirements,
  all proven RED-then-GREEN:

  - **Req A** (`TREADMILL_INTEGRATION`-gated): real-Postgres `read_for_labels`
    cross-host isolation — appends for two labels on two hosts, asserts SQL
    returns only the queried label's rows, ascending offsets, no bleed.

  - **Req B** (unit): `_LeakyLogStore` bypasses the first wall; `_NaiveHostConsumer`
    (no second wall) relays a cross-host row (RED). Real `HostConsumer` refuses
    it and still relays its own label (GREEN). Payload decoded and asserted.

  - **Req C / Sc 1 — CROSS-HOST**: host-A outbox → `OutboxPump` → shared log →
    host-B consumer → relay decoded. `_NoopPublisher` (RED) delivers nothing;
    `_LogPublisher` (GREEN) delivers 2 messages in seq order. Dup-dropped after
    cursor reset.

  - **Req C / Sc 2 — ISOLATION**: host-B writes 4 events while pump not run
    (link cut). Log has 0 rows (RED). Pump restored → drains 4 → all decoded in
    order, exactly once (GREEN). No-adoption: host_store binding unchanged.

  - **Req C / Sc 3 — BROADCAST**: `fanout()` skipped (RED) → 0 deliveries.
    `fanout()` called (GREEN) → one unicast row per subscriber; each host decodes
    its own copy with the composite `dedupKey = f"{subscriber}:{broadcast_id}"`.
    Exactly-once verified on second run.
