---
auto_merge: true
status: active
---

# Plan: Agentic judge-prompt optimizer (ADR-0053)

- **Status:** active
- **Date:** 2026-05-22
- **Related ADRs:** ADR-0053 (agentic optimization via workers), ADR-0052 (human-labeled corpora), ADR-0041 (amended), ADR-0035 (scheduler)

## Goal

Build the optimizer from ADR-0053: a Treadmill workflow that improves a judge's
prompt agentically — propose variants, evaluate each against a held-out slice of
the committed human-labeled gold corpus (via the worker's Claude Code), emit a
gated PR — with **no DSPy library and no raw LLM API key**. Start with the
metric (the evaluation harness); the optimizer role/workflow builds on it.

## Success criteria

- `evaluate_judge_prompt(prompt, examples)` runs a judge prompt over labeled
  examples and returns an accuracy score + per-example predicted-vs-gold — using
  the worker's existing Claude Code, unit-testable without a live LLM.
- (later waves) `role-prompt-optimizer` + `wf-tune-judge-prompts` produce an
  operator-review PR with a tuned prompt + its held-out score.
- First end-to-end run improves the **architect's** held-out score vs its current
  prompt (the ADR-0053 revisit signal if it doesn't).

## Constraints / scope

### In scope
The evaluation harness (this wave); then the optimizer role + workflow; then the
first architect run. The human-labeled gold corpus is committed in-repo
(`docs/analysis/`) so the workflow can read it.

### Out of scope
The DSPy library + any raw `ANTHROPIC_API_KEY` (ADR-0053 rejects both). Validator
judges whose fixes are a prompt-edit (`purpose-articulated`, shipped) or a code
bug (`validation-script-executed` input-starvation; the `result`-vs-`verdict`
parse errors) — those are separate tracks, not optimization.

### Budget
Staged waves. Held-out evaluation spends Claude budget per example × iteration —
keep the held-out slice modest and the iteration count bounded.

## Sequence of work

1. **Evaluation harness** (this wave, dispatched below) — `evaluate_judge_prompt`.
2. **Optimizer role + workflow** — `role-prompt-optimizer` (proposes variants,
   calls the harness, picks the best) + `wf-tune-judge-prompts` registration;
   reads the gold corpus; emits a PR against the rule/role YAML with the score.
3. **First architect run** — point it at `architect-gold-labels.json`; confirm
   the held-out score improves over the current `role-architect` prompt.

## sequence_of_work

```yaml
sequence_of_work:
  - id: judge-eval-harness
    title: Evaluation harness — score a judge prompt against labeled examples (ADR-0053)
    workflow: wf-author
    intent: |
      Build the metric the agentic optimizer (ADR-0053) needs: run a judge
      prompt over labeled examples via the worker's Claude Code and score its
      verdicts against the human gold labels. ADDITIVE — new module + tests +
      the component AGENT.md update. Read
      ``workers/agent/treadmill_agent/validation_runtime.py`` first to match how
      ``run_llm_judge`` composes a prompt and invokes
      ``treadmill_agent.claude_code.run_claude`` + parses the verdict.

      Create ``workers/agent/treadmill_agent/judge_eval.py``:
        - A dataclass ``EvalResult`` with: ``score: float`` (fraction correct),
          ``n: int``, ``correct: int``, and ``per_example: list[dict]`` (each
          ``{index, predicted, gold, correct: bool, error: bool}``).
        - ``def evaluate_judge_prompt(prompt: str, examples: list[dict], *,
          model: str, timeout_seconds: int = 30) -> EvalResult``. Each example is
          a dict with input fields plus a ``gold_verdict`` (str). For each
          example: compose the judge ``prompt`` + the example's inputs (render
          the non-``gold_verdict`` keys into clearly-labelled sections, mirroring
          ``run_llm_judge``'s ``## <Section>`` style — e.g. a ``diff`` key →
          ``## PR diff`` etc.; a generic ``## <key>`` for others), call
          ``claude_code.run_claude`` (import locally as ``run_llm_judge`` does),
          parse the ``verdict`` from the returned JSON envelope (reuse the same
          parsing path ``run_llm_judge`` uses — factor a small shared parser or
          replicate its tolerant parse), compare to ``gold_verdict``
          (case-insensitive string match). A parse failure → that example
          ``error=True`` and counts as incorrect.
        - ``score = correct / n`` (0.0 when ``n == 0``). Verdict comparison is
          general (works for pass/fail validator judges AND accept-as-is/amend
          architect verdicts — it's a string match against ``gold_verdict``).

      Create ``workers/agent/tests/test_judge_eval.py``:
        - Patch ``treadmill_agent.claude_code.run_claude`` (match how existing
          tests patch it) to return canned JSON verdicts for a small example set
          (e.g. 4 examples, 3 matching gold, 1 not) → assert ``score == 0.75``,
          ``n == 4``, ``correct == 3``, and ``per_example`` flags the right one.
        - A parse-failure case: ``run_claude`` returns unparseable output →
          assert that example is ``error=True`` and counts against the score.
        - Empty examples → ``score == 0.0`` (no crash).

      DOCS (ADR-0030 docs-current-with-pr — REQUIRED): update
      ``workers/agent/AGENT.md`` — add ``judge_eval.py`` to Key surfaces (the
      ADR-0053 optimizer's scoring metric) + a Recent-changes entry.
    scope:
      files:
        - workers/agent/treadmill_agent/judge_eval.py
        - workers/agent/tests/test_judge_eval.py
        - workers/agent/AGENT.md
      out_of_scope:
        - workers/agent/treadmill_agent/validation_runtime.py
    validation:
      - kind: deterministic
        description: |
          The eval harness exists and its tests pass.
        script: |
          cd workers/agent \
            && grep -q "def evaluate_judge_prompt" treadmill_agent/judge_eval.py \
            && uv run pytest tests/test_judge_eval.py -q
```

## Risks / unknowns

- **Concurrent session:** scopes `workers/agent` (the other session is in
  `services/api`/`tools/local-adapter`) — low overlap; only `workers/agent/AGENT.md`
  is a possible doc-conflict (resolve at merge if so, as we did for #239).
- **Held-out cost / reward-hacking:** keep the held-out slice modest; operator
  review on every optimization PR (ADR-0053).

## Decisions captured during execution

- **Agentic, not DSPy-library** (ADR-0053): the optimizer uses the worker's
  Claude Code, so there's no managed API key and it's native to the gated-PR
  pipeline. Revisit DSPy only if the agentic loop's held-out score plateaus.

## Post-mortem

_(filled when the plan completes)_
