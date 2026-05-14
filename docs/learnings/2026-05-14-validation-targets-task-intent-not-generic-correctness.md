---
name: validation-targets-task-intent-not-generic-correctness
date: 2026-05-14
status: open
trigger: Operator framing during the ADR-0029 implementation smoke,
  2026-05-14 — *"We want our validation to be more specific than 'tests
  pass'. We are looking for something that lets us exercise the intent
  of the task itself. Running the test suite is something that CI
  should handle."*
session_id: 2ccc6390-0915-4884-8fc8-86692b71895d
captured_via: operator-direct
---

## The lesson

Treadmill's `wf-validate` gate is **not a substitute for CI**. The two
have different scopes:

| Layer | Scope | Question it answers |
| --- | --- | --- |
| **CI** | Generic correctness. Every PR. | "Does the codebase still work?" Linters, type checks, full test-suite, build, dependency-resolve. |
| **wf-validate** | Task-intent verification. Per task. | "Did **this specific task** deliver **its stated outcome**?" The thing the plan-doc said this task would do. |

A wf-validate check phrased as *"`pytest -q` passes"* fails the
intent-targeting test on two counts:

1. **It's not specific to the task.** Every PR in any task would
   want the test suite to pass. That's CI's job.
2. **It doesn't verify intent.** A task that says "Provision an API
   IAM user with a bounded policy" isn't verified by running tests;
   it's verified by inspecting the synthesized CloudFormation template
   to confirm an IAM user exists with the bounded policy.

The intent-targeted version of the same task's validation:

```yaml
validation:
  - kind: deterministic
    description: |
      The synthesized CloudFormation contains exactly one
      AWS::IAM::User named 'treadmill-test-api' with a four-statement
      inline policy whose actions match ADR-0023 §"IAM scope".
    script: |
      cd infra && uv run cdk synth --json TreadmillTestCloudLite \
        | jq -e '[.Resources | to_entries[]
                  | select(.value.Type == "AWS::IAM::User"
                           and .value.Properties.UserName == "treadmill-test-api")]
                 | length == 1'
```

That script verifies the **intent** ("an IAM user with a bounded
policy exists") rather than the **side-effect** ("the test suite
passes").

## Implications for ADR-0029

The starter rules I proposed in the ADR-0029 plan (`python-tests-resolve`,
`uv-lock-resolves`, `cdk-synth-passes`) need re-evaluation against
this principle. Two readings:

**Reading A — they're CI's job, drop them.** `pytest --collect-only`,
`uv lock --check`, `cdk synth` are all generic correctness checks
that CI should run. If Treadmill assumes CI exists, wf-validate
should stay strictly task-intent-targeted and these rules don't
belong in `docs/knowledge-base/rules/`.

**Reading B — they're a class-of-bug enforcement until CI catches
up.** The three hallucinated-API bugs this session (PR #18 boto3,
PR #20 dict-resource, PR #23 SubscriptionFilter) would all have been
caught by these. If CI doesn't run them today, wf-validate is the
stopgap until we add the proper CI step. Once CI lands, drop them.

Either way: **per-task `validation:` blocks in plan docs should be
intent-specific.** The plan-doc parser already enforces at least one
`validation:` entry per task; the operator (or wf-plan, when it
authors plans) should be writing intent-targeted scripts/prompts,
not "`pytest -q` passes".

## How to apply

When authoring a task's `validation:` block:

1. **Re-read the task's `intent:` field.** What's the specific
   outcome this task is supposed to deliver?
2. **Express that outcome as a check.** A deterministic check
   probes a specific assertion ("file X exists", "table Y has
   column Z", "synth produces N resources of type T"). An
   llm-judge check asks "does this PR's diff deliver Q?" where Q
   is named in the task's intent.
3. **Resist the urge to write "tests pass".** If the task adds new
   tests, the deterministic check might be "`pytest path/to/the/new/test.py`"
   — that's intent-targeted (the test for this specific behavior
   passes). But "`pytest -q`" is generic.
4. **Lean into llm-judge for outcome-vs-intent verification.** The
   judge can be told "the task's intent is X; the diff should do
   X; assess whether it does." That's the canonical case for
   semantic validation.

When authoring a **rule** (cross-project, in
`docs/knowledge-base/rules/`):

1. **Rules are class-of-bug enforcement, not task-intent
   verification.** A rule applies via `applies_to:` to many tasks;
   it can't know any single task's intent.
2. **Good rules describe a pattern to enforce.** "Every PR
   touching `*.py` must not contain bare `TODO` without an issue
   reference." That's pattern enforcement.
3. **Bad rules ape CI.** "Every PR must pass `pytest`." That's CI;
   not a rule.

## Related

- ADR-0029 §"Project-agnosticism is load-bearing" — Treadmill ships
  no hardcoded checks; this learning extends that with "and even
  the rules-Treadmill-might-ship shouldn't ape generic CI."
- ADR-0013 — mergeability VIEW already reads `ci.conclusion`
  separately from `validate.decision`. The schema already separates
  the two layers; this learning names why.
- ADR-0006 §"Severity tiers" — `severity: blocking | warning |
  advisory`. Generic correctness ("pytest passes") is plausibly
  `warning` at wf-validate; the intent check is `blocking`.
- [[dual-ingress-paths-need-a-shared-facade]] (yesterday's learning)
  — both are about "the structural seam matters; don't conflate
  layers that should be distinct."

## Operator framing (added 2026-05-14)

Operator call after the first draft: **the ADR is fine.** The lever
for applying this principle is **operator discipline when drafting
plans**, not a change to ADR-0029's architecture or schema. The
parser can't tell intent-targeted apart from generic at parse time;
a human (or a future wf-plan role) does.

What this means concretely:

* **When I author a plan**, every task's `validation:` block names
  the **specific** outcome that task delivers — not "tests pass."
  Re-read the task's intent; phrase the check as the assertion
  that intent obtains.
* **When wf-plan eventually authors plans automatically**, its
  prompt teaches the same discipline. The `validation:` block is
  not boilerplate.
* **The ADR-0029 starter rules** (`python-tests-resolve`,
  `uv-lock-resolves`, `cdk-synth-passes`) stay as drafted — they
  enforce a CLASS of bug (hallucinated APIs) across many tasks via
  `applies_to:`. Class-of-bug rules are legitimate; CI-ape rules
  named per-task are the antipattern.
* **The plan I just authored** (ADR-0029 plan, `docs/plans/2026-05-13-ralph-loop-validation-runner.md`)
  has a mix — some validation entries are intent-targeted ("subprocess
  timeout maps to verdict='error'"); others lead with "tests pass"
  and qualify after. Future plans I author should lean cleanly into
  intent-targeted phrasing.

## Open items

None — operator discipline owns the application. Future
`/learning` invocations should reference this learning when an
author drifts back toward "tests pass."
