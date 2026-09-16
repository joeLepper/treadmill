# 2026-09-15 — ADR-0116 gate-position by merge_target (router Team A)

Adds `gate_position.py` — the ADR-0116 decision the ADR-0118 router consults when it invokes
the evaluator per PR, so the gate WEIGHT matches blast radius:

- `gate_target(merge_target, *, is_promotion=False) -> GateWeight` (pure): `feature-branch`
  task PR → LIGHT (`min_cross_model=1`, no full CI, per ADR-0115); `feature-branch` promotion
  (integration→main) → HEAVY (`min_cross_model=2` + CI); `main` → HEAVY per task. An unknown
  `merge_target` raises rather than silently under-gating to LIGHT.
- `gate_weight_for_repo(session, repo, *, is_promotion=False)` — reads
  `team_configs.merge_target` (absent → the `feature-branch` column default → LIGHT).

`GateWeight` carries `min_cross_model` (evaluator panel breadth) + `run_ci` so the caller wires
both from one decision. Tests: `tests/test_gate_position.py` — RED-then-GREEN foils (the
promotion-is-heavy case REDs a naive "feature-branch → always light"; unknown-target raises;
the exact 1-vs-2 / CI weights pinned). Consumes only `merge_target`; the evaluator-invocation
that applies the weight is the consumer's.
