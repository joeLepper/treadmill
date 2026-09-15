# 2026-09-15 — ADR-0118 phase 1: event→dispatch consumer + per-plan substrate

- `models/plan.py` + migration `20260915_0200`: `plans.substrate` (`legacy`|`router`,
  default `legacy`, check-constrained) — SC6 per-plan substrate binding so the router and the
  legacy coordinator never both act on one plan (no cross-substrate double-dispatch).
- `coordination/dispatch_consumer.py`: `DispatchConsumer`, a background eventbus subscriber
  (mirrors `FabricEventSink`) that turns a delivered task-scoped completion event into the
  next deterministic dispatch — the ADR-0118 wake-gap fix. Increment 1: the
  `github.pr_merged` → dependent-dispatch path, gated on `substrate=='router'`, idempotent via
  the `(task_id, generation)` partial unique index (migration `20260915_0100`). Calls Bert's
  `is_depends_on_satisfied` (judgment stays in the predicate). Dark by default
  (`ROUTER_DISPATCH_ENABLED`).
- Verified: both migrations apply on the full chain; the unique index rejects a duplicate
  author dispatch; the substrate check rejects bad values; the consumer imports and ships dark.
- PENDING (Bert's review pass + next slice): the real-DB integration foils (event → dispatch
  row appears; re-delivery no-ops; legacy-plan skip; durable-delivery / crash-in-between), the
  `ci_result`→re-eval and `evaluator_verdict`→rework handlers, and the worker-assignment policy.
