---
date: 2026-05-14
trigger: pattern
status: crystallized-into-rule-authors-run-validation-before-submitting
related: ADR-0029, ADR-0022, plan-2026-05-13-ralph-loop-validation-runner
last_crystallization_check: 2026-05-17
---

# Learning: Authors must run their own validation before signaling completion

## Trigger

Across one push, multiple wf-author runs submitted PRs whose own
declared validation script failed on the first execution:

* PR #31 (convergence-trigger-third-source) — wrote two new tests
  with a stub signature (`**kwargs` only) that didn't match how the
  consumer actually calls the patched method (positional args). The
  PR's own validation script (`pytest tests/test_consumer_unit.py
  tests/test_dispatch_dedup.py`) raised `TypeError: takes 0
  positional arguments but 3 were given`.
* PR #33 (treadmill-self-hosting-rules) — wrote 7 rule YAMLs +
  scaffold tests. The script-executable test failed (missing +x on
  3 new check scripts); the path-resolution test failed (off-by-one
  in `Path(__file__).parent` chain); the remediation-shape test
  failed because the unquoted YAML key `on:` parses as boolean
  `True` under PyYAML safe_load (YAML 1.1 reserved word).
* PRs #28 + #29 earlier in this session had the same shape — tests
  that wouldn't have passed against the author's own diff.

Joe's framing, verbatim: *"We really need to ensure that authors
check their work before submitting it. We keep finding work that
doesn't pass its own tests."*

## Observation

The wf-author role writes the production code + the tests + the
validation block in the plan — but does not appear to run the
validation script against its own diff before signaling step
completion. The agent is producing **plausible** tests, not
**executed** tests.

This is the recursion bug that ADR-0029's validation runner is
designed to catch *post-submission*, via wf-validate. But author-
side self-check is cheaper: a deterministic subprocess call before
the disposition handler commits + pushes.

## Generalization

When an agent writes both implementation + tests in one step, we
should not trust the tests to pass without running them. The author
operates in a generative mode where syntactic plausibility passes
for verification. The cheapest fix is to make execution the gate,
not the prompt's encouragement.

This mirrors a wider tendency: agents prefer to *describe* a check
rather than *perform* it. The fix is structural, not exhortative.

## Proposed rule

Code-emitting workflows (wf-author + wf-feedback + wf-ci-fix +
wf-conflict) must execute the task's `validation` block — or, when
the rule engine is online, all matching repo rules — against the
working tree before signaling step completion. Failure of any
blocking check → step terminates `failed`, the disposition does
NOT push, and wf-feedback fires off the failure as the convergence
signal.

## Proposed remediation

Two layers, both cheap:

1. **Disposition layer (load-bearing):** `code.py` disposition runs
   each `task_validations` entry's `script` (and, post-ADR-0029,
   matched rules) in the cloned repo before `git push`. Exit ≠ 0 →
   step decision = `fail`, capture stdout/stderr in the step output,
   skip the push, raise to wf-feedback. This is the same primitive
   the validation_runtime already exposes (per PR #29); we just call
   it earlier, from the author disposition rather than only from the
   wf-validate disposition.
2. **Prompt layer (cheap reinforcement):** role-code-author's
   system_prompt gains an explicit instruction: *"Before signaling
   completion, run every script listed in the task's `validation`
   block against your working tree. If any exits non-zero, fix it
   before reporting done."* This is hortatory, not gating, so we
   layer (1) underneath for actual enforcement.

Aligns with [[dispositions-should-be-composable-role-side-effects]]
— self-validation is universal to code-emitting workflows, so it
lives in the disposition, not in workflow-specific branching.

## Notes

* The wf-validate post-merge gate still applies; this learning is
  about *moving the cheapest check earlier in the pipeline*, not
  replacing the runner.
* PR #29 already shipped `validation_runtime` with the subprocess
  primitive. Wiring it into the code-author disposition is small.
* Captured during ADR-0029's first end-to-end smoke, which itself
  surfaced the pattern by running new tests against newly-written
  code for the first time.
* Related: [[validation-targets-task-intent-not-generic-correctness]]
  — author self-check is task-intent validation, not generic CI.
