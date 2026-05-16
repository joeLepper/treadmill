# labeled-examples — corpus for ADR-0041 DSPy optimization

This directory holds the labeled-example corpus that feeds ADR-0041's `wf-tune-judge-prompts` optimizer. One file per validator-rule check, JSONL, one example per line.

## File naming

`<rule-slug>.jsonl` — matches the rule YAML's `name:` field in `docs/knowledge-base/rules/`.

## Example schema

```jsonc
{
  "example_id": "<date>-<task_id_prefix>",         // unique
  "captured_at": "<ISO 8601 timestamp>",            // when the architect overrode (or when an operator labeled this manually)
  "rule_slug": "implementation-conforms-to-diagram",
  "check_id": "implementation-conforms",
  "task_id_prefix": "c5438ed1",                     // 8-char prefix; full task_id available in events table
  "pr_number": 97,
  "plan_doc": "docs/plans/...",
  "task_title": "<task title>",
  "diff_excerpt": "<short summary of files / regions changed; pointer to PR for full diff>",
  "task_spec_excerpt": "<short excerpt of task intent block>",
  "judge_output_actual": {
    "result": "fail-implementation",
    "rationale": "<the judge's actual rationale text — verbatim>"
  },
  "label": "pass | fail-implementation | fail-diagram | uncertain",
  "label_source": "architect-override | operator-manual | crystallized-rule",
  "label_rationale": "<why this is the right label>",
  "architect_run_id": "<UUID>",                     // when source = architect-override
  "architect_verdict": "accept-as-is | amend | supersede | uncertain",
  "commentary": "<freeform notes for human reviewers>"
}
```

## Bootstrap (2026-05-16)

The initial corpus is operator-curated from the 2026-05-15 → 2026-05-16 hands-free session. Most observed validator overrides were `verdict=error` (Pydantic parse failures, not false positives — ADR-0039 makes them non-gating). The clearest true false-positive case was `c5438ed1`, captured as the seed example for `implementation-conforms-to-diagram.jsonl`.

As ADR-0040 lands, additional examples will be appended automatically when the architect overrides a validator rule. The format is append-only so operators can also hand-label edge cases.

## Promotion rules

A new example becomes part of the active corpus on insert (no review gate at write time). The optimization PR is where operator review happens — DSPy never trains on examples without operator visibility downstream. If a labeled example is later judged wrong, remove the line from the file and the next optimization run won't see it.
