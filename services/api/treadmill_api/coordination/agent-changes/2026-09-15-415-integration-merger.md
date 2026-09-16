# 2026-09-15 — ADR-0118 phase 2: integration git merge+push (mechanical → code)

- `coordination/integration_merger.py`: the feature-branch integration merge, moved off the
  coordinator prose (§9.3-feature) into deterministic, IDEMPOTENT code. `MergeOp` +
  `integration_branch_for` (the WHAT); `integrate_task` / `drift` (the git sequence behind an
  injected `GitRunner` — foil-tested with a scripted runner, real subprocess runner is prod);
  `merge_op_for_plan` reads `integration_base` (ADR-0114, main-default).
- ATOMICITY by ancestry, not a lock: before merging, `git merge-base --is-ancestor
  <task_head> origin/<integration_branch>` — if the head is already in the branch (duplicate
  approve, or a retry after a half-landed push) → NO-OP. A half-landed push is safe to retry;
  never double-merges, never force-pushes. A conflict aborts + signals (a worker resolves).
- `drift` merges `origin/<base>` on a base advance and, per ADR-0114, NEVER merges main for a
  non-main base.
- 6 foils green (branch naming; new-head merge+push; already-integrated no-op-no-push;
  conflict abort-no-push-no-force; drift up-to-date; non-main-base never-merges-main).
- PENDING: the real subprocess GitRunner (working clone + gh creds), wiring the merger to the
  approve verdict, and the worker/evaluator launch notification (consuming gate_weight).
