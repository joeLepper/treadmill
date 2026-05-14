---
status: active
trigger: ADR-0029 accepted 2026-05-13; flipped active to dispatch through the plan-merge trigger. Hole 4 (deferred-run redispatch) landed in commit 6588efa earlier today, so dependent tasks should chain cleanly through pr_merged → reevaluate → step.ready emission against the existing pending run.
parent: docs/adrs/0029-ralph-loop-validation-runner-and-rule-engine.md
---

# Plan: Ralph-loop validation runner + rule engine (ADR-0029)

Land the runner that executes `task_validations` + matching rules
against a PR, aggregates worst-wins, feeds `wf-validate.decision`
to the mergeability VIEW, and dispatches `wf-feedback` on failure.
Closes ADR-0006 §"Engine deferred" by folding the rule engine in.

## Goal

After this plan executes:

1. `wf-validate` produces real verdicts (`pass` / `fail` /
   `error`), not the current placeholder string. Mergeability
   VIEW reads them via ADR-0013's projection.
2. The Treadmill repo has 3-5 of its **own** rules at
   `docs/knowledge-base/rules/*.yaml` covering the bug class
   this session surfaced three times (hallucinated APIs / missing
   tests / broken synth). The same rules system catches them
   automatically on every PR.
3. `wf-validate.fail` dispatches `wf-feedback` automatically via
   the third dispatch path (alongside #108's
   review-changes_requested + the github webhook). wf-feedback's
   action role re-authors; pr_synchronize re-fires wf-validate
   against the new SHA; loop converges or hits the 5-attempt
   wf-feedback cap.

## Constraints / scope

### In scope

- Alembic migration 0011: `task_validations.script` (text) +
  `task_validations.prompt` (text) columns + CHECK constraint
  extension.
- `parsers/plan_doc.py`: `TaskValidationCheck` Pydantic gains
  `script` + `prompt` fields.
- `routers/plans.py`: persistence writes the new fields.
- `workers/agent/treadmill_agent/runner_dispositions/validation.py`
  (new module): the validation disposition handler.
- `workers/agent/treadmill_agent/runner.py`: routing keyed on
  `workflow_id == 'wf-validate'`.
- `workers/agent/treadmill_agent/validation_runtime.py`
  (new module): deterministic subprocess executor + LLM-judge
  Claude-spawner.
- `services/api/treadmill_api/coordination/triggers.py`:
  generalize `maybe_dispatch_feedback_on_review_changes_requested`
  into `maybe_dispatch_feedback_on_terminal_failure` covering
  three sources.
- `services/api/treadmill_api/coordination/dispatch_dedup.py`:
  `_build_wf_feedback_key` extended for `validate-run=` namespace.
- `services/api/treadmill_api/coordination/triggers.py`: wf-feedback
  attempt counter caps at 5 per task across all sources.
- `services/api/treadmill_api/starters.py`: drop the placeholder
  prompt for `role-validator`; the role becomes a structural
  artifact (the workflow has 1 step pointing at it, but the
  worker routes by workflow_id and runs the validation handler
  instead of spawning Claude with the role's prompt).
- Per-rule YAML schema additions: `llm_model:`,
  `timeout_seconds:` per Q29.b + Q29.c.
- Rule engine in the worker disposition: load
  `docs/knowledge-base/rules/*.yaml` from the cloned repo; match
  `applies_to:` against the PR's changed files + the task's
  scope; merge with task_validations into one check set.
- 3-5 starter Treadmill rules in
  `docs/knowledge-base/rules/`: covering pytest-runs,
  uv-lock-resolves, cdk-synth-passes. Operator-authored YAML
  pointing at scripts in `tools/rule-checks/<slug>/`.
- Tests: new + existing unit suites; per-disposition contract
  tests; the third dispatch path's dedup + integration.

### Out of scope

- Auto-merge (ADR-0031, separate ADR + plan).
- GitHub check_run posting (Q29.d deferred until GitHub-App
  migration / #109).
- The wf-author empty-diff softening (hole 2 — operator
  deferred).
- Global rules in Treadmill's "orchestrator-side"
  `docs/knowledge-base/rules/` tree as DISTINCT from the managed
  repo's. For Treadmill self-hosting, they're the same tree;
  the global/local distinction matters when #95 (bootstrap
  non-Treadmilled repos) lands. Defer the per-deployment
  configuration surface to that work.
- `fully_remote` execution shape (Q29.g — bunkhouse-precedent
  S3 + cross-account; v0 reads scripts from DB and runs
  in-process).

## Sequence of work

```yaml
# Task 1 (schema-script-prompt-columns) landed via PR #26 on
# 2026-05-14 — alembic 0011 + TaskValidation model change. Dropped
# from this re-fire so wf-author doesn't hit empty-diff on the
# already-merged work. Tasks 2-8 below have had their depends_on
# adjusted: the original task-2 dep (task.schema-script-prompt-columns.pr_merged)
# is satisfied in main, so task 2 (now first in the chain) runs with
# no deps. Git history preserves the original 8-task sequence.
#
# 2026-05-14 update: tasks 2, 3, 4 also landed (PRs #28, #29, #30).
# The smoke had to redeploy after SSO TTL and lost DB state; trimming
# to the unmerged tasks below so wf-author doesn't hit empty-diff on
# already-merged work. Tasks dropped: parser-script-prompt-fields,
# validation-runtime-module, validation-disposition-handler. First task
# in the trimmed chain is convergence-trigger-third-source with no deps.

sequence_of_work:
  - id: convergence-trigger-third-source
    title: wf-validate.fail → wf-feedback as the third dispatch source
    workflow: wf-author
    intent: |
      Generalize
      ``coordination/triggers.maybe_dispatch_feedback_on_review_changes_requested``
      into
      ``maybe_dispatch_feedback_on_terminal_failure``. It accepts
      the failing workflow_id + the failing-decision string + the
      step_completed envelope. Three call sites:
        - wf-review.step.completed where decision='changes_requested'
        - wf-validate.step.completed where decision='fail' or 'error'
        - (existing github webhook path stays via the
          evaluate_triggers path)

      Extend dispatch_dedup ``_build_wf_feedback_key`` for the
      new ``validate-run=<wf_validate_run_id>`` namespace.

      Add a per-task cap on wf-feedback dispatches (Q29.e: 5
      attempts across all sources). Implementation: count
      wf-feedback runs for the task; if >= 5, log
      task.capped and skip dispatch.

      The wf-feedback analyzer prompt extends to handle the new
      input shape: it now reads either a review comment OR a
      validation log excerpt. The action role-code-author prompt
      is unchanged.

      Tests in
      ``services/api/tests/test_consumer_unit.py``:
        - validation fail dispatches wf-feedback with
          validate-run= namespace
        - validation pass does NOT dispatch wf-feedback
        - 5th wf-feedback attempt skips with task.capped log
        - 6th attempt also skips (idempotency on cap)
    scope:
      files:
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/treadmill_api/coordination/dispatch_dedup.py
        - services/api/treadmill_api/coordination/consumer.py
        - services/api/treadmill_api/starters.py
        - services/api/tests/test_consumer_unit.py
        - services/api/tests/test_dispatch_dedup.py
    validation:
      - kind: deterministic
        description: |
          test_consumer_unit.py + test_dispatch_dedup.py pass:
          validation fail dispatches wf-feedback via validate-run=
          namespace; 5-attempt cap holds; dedup builder for the
          new namespace is correctly keyed.
        script: |
          cd services/api && uv run pytest tests/test_consumer_unit.py tests/test_dispatch_dedup.py -q

  - id: role-validator-reclassify
    title: role-validator becomes a structural artifact, not a Claude role
    workflow: wf-author
    intent: |
      Update
      ``services/api/treadmill_api/starters.py``'s ``role-validator``
      definition:

        - output_kind stays ``analysis`` for schema compatibility
          (ADR-0022 still rejects a ``validation`` OutputKind),
          but the prompt is rewritten to be a one-paragraph
          explanation that the worker's validation handler runs
          this role's tasks via subprocess/llm-judge primitives,
          not via Claude Code.
        - system_prompt: "Per ADR-0029, the wf-validate worker
          handles validation via subprocess execution for
          deterministic checks + a separate Claude Code call per
          llm-judge check. This role's system_prompt is unused
          at runtime; it exists only to satisfy the workflow→
          role schema. If you see this text in a Claude session
          output, the runner's wf-validate routing is broken."
        - Update test_starters.py's assertion to expect this
          updated prompt.

      The DB-authoritative configs work (ADR-0028) means this
      prompt update is INERT against deployments unless either
      seed-starters --reset-prompts-from-code or treadmill role
      update lands. Add a note in the runbook
      ``docs/runbooks/edit-a-role-prompt.md`` for the ADR-0029
      cutover.
    scope:
      files:
        - services/api/treadmill_api/starters.py
        - services/api/tests/test_starters.py
        - docs/runbooks/edit-a-role-prompt.md
    validation:
      - kind: deterministic
        description: |
          test_starters.py passes; role-validator's new prompt
          does not contain the word 'placeholder'; the prompt
          explains the runtime routing.
        script: |
          cd services/api && uv run pytest tests/test_starters.py -q \
            && ! grep -i placeholder treadmill_api/starters.py \
            | grep -i "role-validator" \
            && grep -q "ADR-0029" treadmill_api/starters.py

  - id: treadmill-self-hosting-rules
    title: First Treadmill-specific rules in docs/knowledge-base/rules/
    workflow: wf-author
    depends_on:
      - task.role-validator-reclassify.pr_merged
    intent: |
      Author 3-5 starter rules covering this session's bug
      class. Per the project-agnosticism principle, the rules
      live in the repo (this repo) and execute against this
      repo's tooling. Treadmill's runner is generic.

      Suggested rules (operator may adjust):

      1. ``python-tests-resolve.yaml`` — deterministic;
         ``applies_to: ['**/*.py', '**/pyproject.toml']``;
         script: ``uv run pytest --collect-only -q``. Catches
         hallucinated imports + module-load failures.
         severity: blocking.

      2. ``uv-lock-resolves.yaml`` — deterministic;
         ``applies_to: ['**/pyproject.toml']``;
         script: ``uv lock --check``. Catches hallucinated
         dependency names (PR #18's boto3 vs botocore).
         severity: blocking.

      3. ``cdk-synth-passes.yaml`` — deterministic;
         ``applies_to: ['infra/**/*.py']``;
         script: ``cd infra && uv run cdk synth -q``. Catches
         hallucinated CDK imports (PR #23's SubscriptionFilter
         location) + token-shape mistakes (PR #20's unhashable
         dict).
         severity: blocking.

      4. ``no-todo-without-issue.yaml`` — llm-judge;
         ``applies_to: ['**/*.py']``;
         prompt: "Does the diff contain TODO/FIXME/XXX comments
         that lack an issue or task reference? Acceptable forms:
         TODO(#NNN), TODO(taskname). Bare TODO is fail."
         severity: warning.

      5. ``adr-references-resolve.yaml`` — llm-judge;
         ``applies_to: ['docs/adrs/*.md', 'docs/plans/*.md']``;
         prompt: "Does this diff cite ADR-NNNN numbers that
         actually exist in docs/adrs/?"
         severity: advisory.

      Each rule's check script (if deterministic) lives at
      ``tools/rule-checks/<rule-slug>/check.sh``.

      Tests in
      ``services/api/tests/test_rules_schema.py`` (new): each
      YAML parses against ADR-0006's schema; ``applies_to:``
      globs are valid; deterministic scripts exist on disk;
      llm-judge prompts are non-empty.
    scope:
      files:
        - docs/knowledge-base/rules/python-tests-resolve.yaml
        - docs/knowledge-base/rules/uv-lock-resolves.yaml
        - docs/knowledge-base/rules/cdk-synth-passes.yaml
        - docs/knowledge-base/rules/no-todo-without-issue.yaml
        - docs/knowledge-base/rules/adr-references-resolve.yaml
        - tools/rule-checks/python-tests-resolve/check.sh
        - tools/rule-checks/uv-lock-resolves/check.sh
        - tools/rule-checks/cdk-synth-passes/check.sh
        - services/api/tests/test_rules_schema.py
    validation:
      - kind: deterministic
        description: |
          test_rules_schema.py passes for all 5 starter rules;
          each rule YAML parses against ADR-0006's schema;
          referenced check.sh scripts exist with executable bit.
        script: |
          cd services/api && uv run pytest tests/test_rules_schema.py -q \
            && for f in docs/knowledge-base/rules/python-tests-resolve.yaml \
                       docs/knowledge-base/rules/uv-lock-resolves.yaml \
                       docs/knowledge-base/rules/cdk-synth-passes.yaml \
                       docs/knowledge-base/rules/no-todo-without-issue.yaml \
                       docs/knowledge-base/rules/adr-references-resolve.yaml; do \
                 test -f "../../$f" || { echo "missing: $f"; exit 1; }; \
               done

  - id: smoke-validation
    title: End-to-end smoke — deliberately reintroduce a bug; watch loop converge
    workflow: wf-validate
    depends_on:
      - task.treadmill-self-hosting-rules.pr_merged
    intent: |
      Manually open a PR that reintroduces one of the
      hallucinated-API bugs (e.g., temporarily revert PR #18's
      hotfix — change ``-botocore`` to ``-boto3`` in
      pyproject.toml). Watch the chain:

        1. pr_opened → wf-author + wf-review + wf-validate fan-out
        2. wf-author has nothing to do (we authored manually) —
           OR if wf-author runs first, it produces no diff and we
           open the PR ourselves.
        3. wf-review approves (the prompt matches the spec
           visually).
        4. **wf-validate runs `uv-lock-resolves` rule → fail.**
        5. wf-validate.fail → wf-feedback dispatched.
        6. wf-feedback analyzer reads the uv-lock failure;
           directive: "the boto3 package doesn't exist; use
           botocore."
        7. wf-feedback action re-authors with botocore; pushes.
        8. pr_synchronize → wf-validate re-fires.
        9. uv-lock-resolves passes. wf-validate.decision='pass'.
        10. mergeability VIEW flips to mergeable.
        11. (Operator merges; ADR-0031 will auto-merge when
            it lands.)

      Document the cycle count + token spend so we know the
      Ralph-loop's economics under real conditions.
    scope:
      files:
        - docs/handoffs/2026-05-14-ralph-loop-first-smoke.md
    validation:
      - kind: deterministic
        description: |
          The handoff doc exists at the named path and contains
          the cycle count + observed token spend.
        script: |
          test -f docs/handoffs/2026-05-14-ralph-loop-first-smoke.md \
            && grep -qi "cycle" docs/handoffs/2026-05-14-ralph-loop-first-smoke.md \
            && grep -qi "token" docs/handoffs/2026-05-14-ralph-loop-first-smoke.md
```

## Operator action items (post-implementation)

After this plan completes, the following are operator-mediated:

* Author additional rules as patterns emerge. The five starter
  rules cover the bug class observed this session; more will
  surface.
* The ADR-0031 auto-merge ADR. Until that lands, the operator
  still merges PRs even when mergeability=mergeable.
* Bootstrap non-Treadmilled repos (#95) — when other repos enter
  the system, decide whether they inherit Treadmill's global rules
  or carry only their own per-repo rules. Q29.a's resolution
  says both apply; the exact selector for "global" rules (a
  per-deployment YAML? Treadmill's own
  `docs/knowledge-base/rules/` propagated to managed-repo
  workspaces? a CDN?) is a #95 question.

## Deferred items (per Q29 resolutions)

* GitHub check_run posting (Q29.d) — until GitHub-App migration.
* `fully_remote` runner shape (Q29.g) — until that deployment
  topology lands.
