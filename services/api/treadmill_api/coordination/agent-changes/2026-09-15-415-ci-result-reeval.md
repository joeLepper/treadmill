# 2026-09-15 — ADR-0118 phase 1: ci_result→re-eval wiring (TRACE 2)

- `models/evaluator_dispatch.py` + migration `20260915_0300`: `evaluator_dispatches` table,
  UNIQUE(task_id, head_sha) — the eval-path analog of the `(task_id, generation)` index.
- `dispatch_consumer.py`: wired Bert's `on_ci_result` into the `("task","ci_result")` classify
  branch (substrate-gated via `_is_router_task`), with `_dispatch_evaluator(task_id, head_sha)`
  as the injected write. The first ci_result whose required checks are terminal+passing fires
  the evaluator and records a row; a trailing non-required suite's ci_result no-ops on the
  unique constraint (evaluator fires exactly once per head); a rework push is a new head → a
  new evaluation.
- Verified: migration chain applies; the unique constraint rejects a duplicate (task, head) and
  accepts a new head; consumer imports.
- PENDING (Bert): the trailing-suite integration foil (first ci_result fires once; a later
  non-required suite's ci_result no-ops) against the dedup record.
