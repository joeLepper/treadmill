---
auto_merge: false
status: draft
---

# Plan: Wave 4 — generalize the prompt optimizer to all role types (ADR-0056)

- **Status:** draft (per ADR-0056, design accepted; implementation gated on
  one verified end-to-end run of the narrow ADR-0053 optimizer on
  role-architect to confirm the closed-loop + the canary path)
- **Date:** 2026-05-27
- **Related ADRs:** ADR-0056 (this plan's design), ADR-0053 (Wave 2/3
  narrow optimizer), ADR-0052 (human-labeled corpora), ADR-0020 (token-usage
  tracking — the durable signal Wave 4 leans on for retrospective scoring).

## Goal

Make `role-prompt-optimizer` + `wf-tune-judge-prompts` (renamed
`wf-tune-role-prompts`) work for **any role type**, not just judges. The
narrow ADR-0053 optimizer scores via labeled-corpus verdict equality —
fine for judges. Author + procedural roles need a different scorer:
**retrospective downstream-outcome signal** (did the role's output ride
clean to merge, or trigger wf-feedback loops?).

## Success criteria

- `evaluate_role_retrospectively(role_id, window) → RetroEvalResult` works:
  given a role + time window, queries `workflow_run_steps` /
  `workflow_runs` for the role's touches and returns a per-task summary
  (`clean / looped / mean_tokens`) + an aggregate score.
- The optimizer ROLE PROMPT detects role type and picks the right scorer
  (verdict-eval for judges, retrospective for authors/procedural).
- The workflow slug is renamed `wf-tune-role-prompts`; the old
  `wf-tune-judge-prompts` slug is aliased + deprecation-warned (existing
  schedule row keeps working).
- The cron schedule(s) cover at minimum:
  `role-architect` (Saturday — already scheduled),
  `role-code-author` (Sunday),
  `role-reviewer` (Monday),
  `role-validator` (Tuesday).
- A first operator-triggered run for `role-code-author` (Wave 4 canary)
  produces a variant prompt + retrospective score + PR-emission OR
  `"NO IMPROVEMENT"`.

## Constraints / scope

### In scope
The retrospective scorer + the optimizer-role prompt routing + the
workflow rename (with alias) + the new schedule rows. Same auto_merge:true
worker dispatch pattern.

### Out of scope
- **Canary vs control sampling** (variant-vs-current on real traffic) —
  this is ADR-0056's Wave 4b. v1 of this plan scores the CURRENT prompt
  retrospectively + proposes a variant; the operator reviews whether the
  rationale is sound (no live-traffic comparison until 4b).
- Modifying judge_eval.py — Wave 1's; reuse as-is.
- Restructuring starters.py or seed/schedules.py beyond adding the new
  rows + the workflow alias.

### Budget
3 sequenced tasks. `auto_merge: false` (the optimizer's role prompt and
the retrospective scorer SQL are load-bearing — the operator wants to
review them before they merge):

1. `wave4-retrospective-scorer` — new
   `workers/agent/treadmill_agent/role_eval.py` with
   `evaluate_role_retrospectively`. Tests scope the SQL against a seeded
   in-memory DB. Pure addition; no existing code touched.
2. `wave4-optimizer-prompt-routing` — extend the role-prompt-optimizer's
   prompt in `starters.py` to detect role type + dispatch to the right
   scorer. Updates existing starter; minimal diff.
3. `wave4-schedules-and-alias` — rename `wf-tune-judge-prompts` →
   `wf-tune-role-prompts` (add the new workflow + alias the old slug);
   add `SEED_SCHEDULES` rows for the three additional roles
   (role-code-author Sunday, role-reviewer Monday, role-validator Tuesday).

## sequence_of_work

```yaml
sequence_of_work:
  - id: wave4-retrospective-scorer
    title: Retrospective role scorer — outcome signal from runtime data (ADR-0056)
    workflow: wf-author
    intent: |
      Add ``workers/agent/treadmill_agent/role_eval.py`` with
      ``evaluate_role_retrospectively(role_id, *, window_seconds, session)
      -> RetroEvalResult``. Read first:
      ``workers/agent/treadmill_agent/judge_eval.py`` — ``EvalResult`` is
      the sibling pattern (a dataclass returning a score + per-example
      detail). Mirror its shape: ``RetroEvalResult`` with ``score: float``
      (clean_fraction - 0.5 * looped_fraction in [-0.5, 1]), ``n: int``
      (tasks touched in window), ``per_task: list[dict]`` (task_id, runs,
      feedback_runs, amend_runs, merged, tokens).

      The SQL ([[ADR-0056]] section "Retrospective signal — what it
      actually measures"): query ``workflow_run_steps`` joined to
      ``workflow_runs`` + ``tasks`` for the role's completed steps within
      the window; aggregate per task; compute clean / looped flags.

      Tests at the exact path ``workers/agent/tests/test_role_eval.py``:
      seed a small in-memory SQLite (mirror the existing test pattern
      for SQL-backed code), insert 3 fake tasks with varying outcomes
      (one clean ≤3 runs + pr_merged; one looped ≥1 wf-feedback; one
      mixed), assert the per_task dicts + the aggregate score match
      hand-computed expected values.

      DOCS: ``workers/agent/AGENT.md`` — note ``role_eval.py`` (the
      retrospective scorer for non-judge roles).
    scope:
      files:
        - workers/agent/treadmill_agent/role_eval.py
        - workers/agent/tests/test_role_eval.py
        - workers/agent/AGENT.md
      out_of_scope:
        - workers/agent/treadmill_agent/judge_eval.py
        - services/api/
        - cli/
    validation:
      - kind: deterministic
        description: |
          The scorer exists and its tests pass.
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          grep -q "def evaluate_role_retrospectively" "$ROOT/workers/agent/treadmill_agent/role_eval.py" \
            && cd "$ROOT/workers/agent" && uv run pytest tests/test_role_eval.py -q

  - id: wave4-optimizer-prompt-routing
    title: role-prompt-optimizer prompt — detect role type + route to right scorer (ADR-0056)
    workflow: wf-author
    depends_on: [task.wave4-retrospective-scorer.pr_merged]
    intent: |
      Extend the ``role-prompt-optimizer`` role's prompt in
      ``services/api/treadmill_api/starters.py`` (find the role entry
      seeded by ADR-0053 Wave 2). The current prompt assumes a judge +
      gold corpus; extend it to:

        1. Detect the target role's "type" from its id (heuristic: ids
           containing ``judge``/``architect``/``reviewer``/``validator``
           are JUDGE; ids containing ``author`` are AUTHOR; others are
           PROCEDURAL). The role's prompt should be explicit about this
           lookup — don't introduce a new schema field.
        2. For JUDGE: call ``evaluate_judge_prompt(...)`` as today.
        3. For AUTHOR or PROCEDURAL: call
           ``evaluate_role_retrospectively(role_id,
           window_seconds=86400*30)`` — last 30 days of touches.
        4. Propose ONE variant. Score it on the same evaluation. Emit a
           PR with a unified diff against the role's prompt definition
           (in ``starters.py``) + a structured output envelope (see
           ADR-0053 Wave 2 spec — same fields, just `metric` field added
           to indicate which scorer was used).

      The role prompt is the only file change here (idempotent re-seeding
      via the operator CLI ``treadmill workflows seed-starters`` rewrites
      the role row on the next run; the optimizer worker uses the new
      prompt at next dispatch).

      Tests at ``services/api/tests/test_optimizer_prompt_routing.py``:
      a structural test that loads the role definition from starters.py
      and asserts the prompt text mentions both
      ``evaluate_judge_prompt`` and ``evaluate_role_retrospectively`` —
      this catches drift where someone updates the prompt and accidentally
      drops the routing logic.
    scope:
      files:
        - services/api/treadmill_api/starters.py
        - services/api/tests/test_optimizer_prompt_routing.py
        - services/api/AGENT.md
      out_of_scope:
        - workers/agent/
        - services/api/treadmill_api/seed/schedules.py
    validation:
      - kind: deterministic
        description: |
          Optimizer prompt mentions both scorers; test passes.
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          grep -q "evaluate_judge_prompt" "$ROOT/services/api/treadmill_api/starters.py" \
            && grep -q "evaluate_role_retrospectively" "$ROOT/services/api/treadmill_api/starters.py" \
            && [ -f "$ROOT/services/api/tests/test_optimizer_prompt_routing.py" ] \
            && cd "$ROOT/services/api" && uv run pytest tests/test_optimizer_prompt_routing.py -q

  - id: wave4-schedules-and-alias
    title: Schedules for the three additional roles + workflow slug rename (ADR-0056)
    workflow: wf-author
    depends_on: [task.wave4-optimizer-prompt-routing.pr_merged]
    intent: |
      (1) Add ``wf-tune-role-prompts`` to ``starters.py`` as the new
      canonical workflow slug (mirror ``wf-tune-judge-prompts``'s shape).
      Keep ``wf-tune-judge-prompts`` registered too, both pointing at the
      same role/step config — the old schedule row stays valid.
      Docstring on the old slug: ``"Deprecated alias for
      wf-tune-role-prompts; kept for the role-architect schedule
      registered before ADR-0056."``.

      (2) Add three new ``SEED_SCHEDULES`` entries in
      ``services/api/treadmill_api/seed/schedules.py``:
        - ``role-code-author`` — Sunday 20:00 Pacific (``0 20 * * 0``).
          Wait — that collides with crystallization. Use Sunday 21:00
          (``0 21 * * 0``) to avoid the same-tick race.
        - ``role-reviewer`` — Monday 21:00 Pacific (``0 21 * * 1``).
        - ``role-validator`` — Tuesday 21:00 Pacific (``0 21 * * 2``).
      Each ``payload_template`` carries ``repo: "joeLepper/treadmill"`` +
      ``role_id: "<role>"`` (note: ``role_id`` replaces ``judge_role`` in
      the new schema — but the optimizer prompt accepts both for
      backwards compat with the existing role-architect schedule).

      Tests at
      ``services/api/tests/test_wave4_schedules_and_alias.py``: assert
      both workflow slugs present in starters.py; assert 4 schedules
      with the right ``role_id`` (and ``judge_role`` alias for architect).

      DOCS: ``services/api/AGENT.md`` — Wave 4 sched + workflow alias.
    scope:
      files:
        - services/api/treadmill_api/starters.py
        - services/api/treadmill_api/seed/schedules.py
        - services/api/tests/test_wave4_schedules_and_alias.py
        - services/api/AGENT.md
      out_of_scope:
        - workers/agent/
    validation:
      - kind: deterministic
        description: |
          Both workflow slugs present; 4 schedules; tests pass.
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          grep -q "wf-tune-role-prompts" "$ROOT/services/api/treadmill_api/starters.py" \
            && grep -q "wf-tune-judge-prompts" "$ROOT/services/api/treadmill_api/starters.py" \
            && grep -q "role-code-author" "$ROOT/services/api/treadmill_api/seed/schedules.py" \
            && grep -q "role-reviewer" "$ROOT/services/api/treadmill_api/seed/schedules.py" \
            && grep -q "role-validator" "$ROOT/services/api/treadmill_api/seed/schedules.py" \
            && [ -f "$ROOT/services/api/tests/test_wave4_schedules_and_alias.py" ] \
            && cd "$ROOT/services/api" && uv run pytest tests/test_wave4_schedules_and_alias.py -q
```

## Risks / unknowns

- **Low-volume roles** lack statistical power for retrospective scoring.
  Mitigation: the schedule per-role stagger means low-volume roles only
  re-tune weekly with a 30-day window — should accumulate enough touches
  for most roles.
- **Canary mechanism deferred to Wave 4b** — v1 scores the current prompt
  + proposes a variant + operator reviews. No live-traffic A/B until 4b.
- **Schedule day collisions** — Sunday 20:00 is crystallization's; I moved
  role-code-author to Sunday 21:00 to avoid same-tick races.

## Post-mortem

_(filled when the plan completes)_
