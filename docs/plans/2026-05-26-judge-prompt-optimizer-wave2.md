---
auto_merge: true
status: active
---

# Plan: ADR-0053 Wave 2 — role-prompt-optimizer + wf-tune-judge-prompts (operator-triggered first)

- **Status:** active
- **Date:** 2026-05-26
- **Related ADRs:** ADR-0053 (agentic judge-prompt optimization via workers), ADR-0052 (human-labeled corpora), ADR-0041 (amended).
- **Builds on:** ADR-0053 Wave 1 (eval harness `workers/agent/treadmill_agent/judge_eval.py` — `evaluate_judge_prompt` + `EvalResult`, already shipped).

## Goal

Wire up the **agentic judge-prompt optimizer** so we can periodically re-tune
judge role prompts against the labeled gold corpus, raising held-out accuracy
and reducing how often a task has to loop through wf-feedback before merging.

This wave defines the **role + workflow + seeds** so the optimizer can be
**operator-triggered** end-to-end against `role-architect` (the highest-leverage
judge — its amend/supersede/accept verdicts are the hot path for loop count).
A follow-on wave (Wave 3) seeds the cron schedule once we've verified one
end-to-end run.

## Success criteria

- A new role **`role-prompt-optimizer`** + workflow **`wf-tune-judge-prompts`**
  are seeded via `starters.py`; the workflow has a `WorkflowVersion`.
- Triggering `wf-tune-judge-prompts` for `role-architect` end-to-end:
  loads the gold corpus, scores the current prompt, proposes one variant,
  scores it, and emits a PR with a unified diff against the role's rule YAML
  + the before/after scores — OR a `"NO IMPROVEMENT"` summary if the variant
  doesn't beat the current prompt by ≥0.05.
- No raw LLM API key (ADR-0053 — uses worker Claude Code only).
- Existing tests stay green; new tests cover the role/workflow seeding +
  the optimizer's structural-output contract.

## Constraints / scope

### In scope
The role + workflow definitions, their seeders, the optimizer-role prompt
text, tests, docs. Operator-triggered first; **no schedule seeded yet**
(Wave 3).

### Out of scope
- The schedule (Wave 3 — after one verified end-to-end run).
- Optimizing judges other than `role-architect` (rotate in Wave 3+).
- Proposing >1 variant per run (greedy single-variant for v1).
- DSPy library (ADR-0053 rejects it).
- Modifying `judge_eval.py` (Wave 1's; reuse as-is).

### Budget
One task, `auto_merge: true`. Touches `services/api/treadmill_api/starters.py`
+ tests + docs. **No** changes to existing role/workflow definitions or to
`judge_eval.py`. Validation script uses **absolute paths** (no `cd ../X`
typos per the recent post-mortem).

## sequence_of_work

```yaml
sequence_of_work:
  - id: role-prompt-optimizer
    title: Seed role-prompt-optimizer + wf-tune-judge-prompts (ADR-0053 Wave 2)
    workflow: wf-author
    intent: |
      Define the optimizer role + workflow, seed them via ``starters.py``,
      and add tests + docs. **No schedule** (Wave 3). Read first:
        * ``docs/adrs/0053-agentic-judge-prompt-optimization-via-workers.md``
          for the design intent.
        * ``services/api/treadmill_api/starters.py`` — find the existing
          ``wf-crystallize-learning`` definition (~line 1115) as the
          template for adding a new role + workflow.
        * ``workers/agent/treadmill_agent/judge_eval.py`` — ``EvalResult`` +
          ``evaluate_judge_prompt`` are the metric (already shipped).
        * ``tools/load-analysis-corpus.sh`` — how the worker pulls the
          labeled corpus from S3 via ``TREADMILL_CORPUS_S3_URI``.

      (1) ROLE — add ``role-prompt-optimizer`` to ``starters.py`` (the
      ``_ROLE_SEEDS`` block — match the shape of existing roles). Its prompt
      MUST be **literally this text** (operator-authored — do not paraphrase):

      ```
      You are role-prompt-optimizer. Given a judge role's current prompt +
      a held-out labeled corpus, propose ONE improved variant of the judge
      prompt and report whether it scores higher than the current one.

      Inputs (provided via the step's payload + the workspace):
        - ``judge_role``: the judge role id (e.g. ``role-architect``).
        - ``judge_prompt_path``: the file containing the current prompt
          (e.g. ``docs/knowledge-base/rules/<rule>.yaml`` or the role's
          definition in ``services/api/treadmill_api/starters.py`` — find
          the canonical source).
        - ``corpus_s3_uri``: the S3 URI for the labeled corpus.

      Steps:
        1. Pull the corpus locally:
           ``TREADMILL_CORPUS_S3_URI=<corpus_s3_uri> tools/load-analysis-corpus.sh pull``
           (uses the worker's AWS creds). Read the labeled JSON.
        2. Split deterministically: the last 30% of rows by index are
           held-out; the first 70% are reference (do NOT use them for
           scoring — only for understanding what kinds of cases the judge
           sees).
        3. Read the current prompt from ``judge_prompt_path``. Score it on
           the held-out slice via ``evaluate_judge_prompt(prompt, examples,
           model=<judge_role's model>)``. Record ``current_score``.
        4. Propose ONE refined variant — a SMALL, targeted edit (sharpen
           one criterion, fix one ambiguity, add one missing failure mode).
           Do NOT rewrite the prompt wholesale. Show the unified diff.
        5. Score the variant on the same held-out slice. Record
           ``variant_score``.
        6. If ``variant_score - current_score >= 0.05``: open a PR with the
           rule-YAML patch + the rationale + both scores. Otherwise output
           ``"NO IMPROVEMENT"`` with the scores + a one-paragraph rationale.

      Output envelope (JSON, in ``payload``):
      {
        "judge_role": "<role-id>",
        "current_score": <float 0..1>,
        "variant_score": <float 0..1>,
        "improvement": <float>,
        "verdict": "improvement" | "no_improvement",
        "patch": "<unified diff text>" | null,
        "rationale": "<one paragraph>"
      }

      No silent cross-account fallback (ADR-0055). Never paste secret
      values to chat — the corpus loader uses environment-driven AWS creds.
      ```

      (2) WORKFLOW — add ``wf-tune-judge-prompts`` to ``starters.py``
      (the workflow-definitions block, mirror the ``wf-crystallize-learning``
      structure ~line 1115). Single step ``optimize`` bound to
      ``role-prompt-optimizer``. Workflow's payload schema accepts
      ``judge_role`` + ``corpus_s3_uri``.

      (3) WORKFLOWVERSION — ensure ``seed_starters_if_empty`` registers a
      ``WorkflowVersion`` row for ``wf-tune-judge-prompts`` (version 1).
      The existing pattern in ``seed()`` should handle it automatically if
      the workflow is added correctly — verify with a unit test that asserts
      ``WorkflowVersion`` exists after seeding.

      (4) NO SCHEDULE — leave the schedule for Wave 3. The operator will
      trigger ``wf-tune-judge-prompts`` once manually to verify end-to-end
      before automating.

      (5) TESTS — add a NEW test file at the EXACT path
      ``services/api/tests/test_seed_prompt_optimizer.py`` (path matters —
      the validation script targets it specifically):
        * Run ``seed_starters_if_empty`` against an in-memory or test DB
          (mirror existing seeding tests' fixture pattern).
        * Assert: ``Role`` row exists with id ``role-prompt-optimizer``;
          ``Workflow`` row exists with slug ``wf-tune-judge-prompts``;
          exactly one ``WorkflowVersion`` for it; the role's prompt
          contains the marker string ``"role-prompt-optimizer"`` (sanity).

      (6) DOCS (ADR-0030 — REQUIRED): update ``services/api/AGENT.md`` —
      note the new role + workflow under "Key surfaces", referencing
      ADR-0053. Note in ``workers/agent/AGENT.md`` if the optimizer's
      execution involves a new path on the worker side (it doesn't if the
      role just drives Claude via existing role-step plumbing — verify and
      only update if needed).
    scope:
      files:
        - services/api/treadmill_api/starters.py
        - services/api/tests/test_seed_prompt_optimizer.py
        - services/api/AGENT.md
      out_of_scope:
        - workers/agent/treadmill_agent/judge_eval.py
        - docs/knowledge-base/rules/
        - docs/knowledge-base/roles/
        - cli/
        - infra/
    validation:
      - kind: deterministic
        description: |
          The new role + workflow are seeded; the dedicated test file
          (exact path) passes.
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          grep -q "role-prompt-optimizer" "$ROOT/services/api/treadmill_api/starters.py" \
            && grep -q "wf-tune-judge-prompts" "$ROOT/services/api/treadmill_api/starters.py" \
            && [ -f "$ROOT/services/api/tests/test_seed_prompt_optimizer.py" ] \
            && cd "$ROOT/services/api" && uv run pytest tests/test_seed_prompt_optimizer.py -q
```

## Risks / unknowns

- **Where the judge's "current prompt" lives** (rule YAML under
  `docs/knowledge-base/rules/` vs inline string in `starters.py`): the
  optimizer's role prompt instructs the worker to **find the canonical
  source** rather than assume. The output patch's target file falls out of
  that.
- **Corpus availability**: needs `TREADMILL_CORPUS_S3_URI` set on the worker.
  Verify the worker's env carries it (or add it during operator setup).
- **Cost per run**: the optimizer makes N+1 LLM calls (N held-out examples ×
  2 prompts + 1 for variant proposal). For `role-architect` with ~40 gold
  examples, ~80 LLM calls per run. Reasonable for a manually-triggered first
  run; will need budgeting before scheduling (Wave 3 decision).
- **Schedule deliberately omitted** — operator triggers Wave 2's first run
  manually + verifies the PR shape before Wave 3 adds the cron.

## Post-mortem

_(filled when the plan completes)_
