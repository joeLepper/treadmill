# 2026-09-15 — ADR-0118 phase 2: launch-notify (server half)

- `dispatch_consumer.py`: on a real dispatch, `_dispatch` now emits the LAUNCH signal —
  `persist_and_publish` a `task.ready` event (the incumbent worker-launch marker; the retired
  SQS→Docker path's replacement) in the SAME transaction as the task_executions row, so the
  dispatch record + the durable Event commit atomically. The assigned worker (subscribed by
  its label) consumes it and fetches its brief. Injected via `Dispatcher` (built from
  `app.state.publisher` in `make_dispatch_consumer`); no dispatcher → record-only (dark/test).
- Idempotent by construction: a re-delivery no-ops on the (task_id, generation) unique index
  BEFORE the publish, so no duplicate launch. Foil pins task.ready fires exactly once.
- 16 integration foils green (record-only paths unchanged; the launch foil added).
- PENDING (activate): the worker-side subscription (workers subscribe to `/ws?worker_label=`
  and consume task.ready + fetch brief, mirroring the coordinator's coordinator_label WS), the
  evaluator-launch (carrying Bert's gate_weight), and the app.py lifespan start (behind
  ROUTER_DISPATCH_ENABLED).
