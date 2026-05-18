---
date: 2026-05-18
trigger: pattern
status: captured
related: ADR-0031 (auto-merge), ADR-0036 (hands-free review), ADR-0042 (validate.override), PR #149
---

# Learning: wf-ci-fix's analyzer skips the actual CI logs and reports "already complete"

## Trigger

PR #149 (task `9b9dffa8`, auto-deploy plan task 1) had wf-validate fail (spurious LLM-judge), architect verdict `accept-as-is`, ADR-0042 fired correctly so the mergeability VIEW projected `validate_decision=pass` + `review_decision=approved`. But CI failed: `tools/local-adapter/test_image_build.py` (4 tests) + `test_runtime_dev_local.py` because adding entries to `_AWS_OUTPUTS` makes those keys required in every loaded YAML via `_REQUIRED_AWS_KEYS = tuple(k for k, _ in _AWS_OUTPUTS)`, and the existing `_valid_yaml_dict()` fixtures don't carry the new keys.

The system dispatched `wf-ci-fix` at least four times. Every analyzer step produced output beginning with phrases like:

- *"Perfect! The task has already been completed in the most recent commit."*
- *"Perfect! The implementation is complete."*
- *"Looking at the code, I can see that the task has already been implemented..."*

None of the analyzer outputs contain log excerpts, test failure tracebacks, file paths from the failure, or the words "test_image_build" / "test_runtime_dev_local" / "fixture." The downstream code-author then dutifully concludes "task already in place" and the loop spins.

## Observation

`role-ci-analyzer`'s prompt (services/api/treadmill_api/starters.py:416) says:

> Input: the failing check name + URL + its logs (fetch with ``gh run view --log-failed <run-id>``). Action: identify the failure type ... and the smallest fix — which file to edit, what change.

The analyzer is meant to **fetch** the logs and **diagnose**. In practice it skips the fetch and substitutes its own diagnosis from the code state: "the code change for the original task is in place, therefore nothing is broken." This is the same anti-pattern as ADR-0042's "implementation is already in place" failure on the code-author side — but on the analyzer side, where there's no architect-remediation to break the loop.

The shape:
- The analyzer is told it has an *input* (logs + URL).
- The input may be partially absent or buried in the prompt context.
- The analyzer falls back to inspecting the codebase — which is exactly what makes it conclude "nothing wrong."
- The downstream code-author reads the analyzer's report and acts on the surface signal.

The wf-ci-fix workflow is dispatched **because CI failed**. The system already knows there is a problem. An analyzer that reports "no problem" is contradicting load-bearing system state.

## Generalization

When an analyzer role is dispatched *in response to* a known failure signal (CI failed, validate failed, deadlock), the analyzer must not be allowed to report "no problem." The dispatch trigger itself is proof of the problem. The analyzer's job is to *characterize the failure*, not to re-litigate whether it exists.

Sibling shape (same anti-pattern, different role): ADR-0042's `2026-05-16-architect-remediation-must-name-paths-and-verbs.md` (architect verdicting `accept-as-is` when the work was incomplete). Same upstream fix: make the prompt forbid certain output shapes that contradict the dispatch reason.

## Proposed rule

`role-ci-analyzer`'s prompt must require:
1. **Always start by running** `gh run view --log-failed <run-id>` — refuse to proceed without log content.
2. **Never emit "task is already complete," "implementation is in place," or similar.** wf-ci-fix is dispatched because CI failed; emitting "no problem" is invalid output. If the analyzer literally cannot find a failure in the logs, the only valid output is `blocked: <what's missing from the input — run id, log URL, etc.>`.
3. **Name every failing test file from the traceback.** Tests fail in files; the failing files must appear in the directive. When a CI failure is a *transitive* consequence of the original change (e.g., a new key in `_AWS_OUTPUTS` breaks downstream fixtures), the analyzer must spell out the chain — "X change made Y key required; Z fixture lacks the key."

`role-code-author` (wf-ci-fix path): when the analyzer's directive names test files outside the original task's `scope.files`, treat the directive's named files as authoritative (same precedent as ADR-0042's architect-remediation override).

## Proposed remediation

Two prompt edits in `services/api/treadmill_api/starters.py`:

1. `role-ci-analyzer` — add a forbid-list of phrases ("task is already complete," "implementation is in place," "no issue found") that the analyzer must NOT emit. Add an explicit "you were dispatched because CI is failing — start with the log fetch" instruction. Specify "name every failing test file by full path."

2. `role-code-author` (the wf-ci-fix path is the same role as wf-feedback) — extend the existing ADR-0042 anti-"already in place" forbid to cover the wf-ci-fix case: when CI is failing and a directive names test files, those files are MANDATORY deliverables regardless of whether the original `scope.files` covered them.

Follow-up: capture this as a small PR after the auto-deploy plan unblocks (so as not to add more in-flight churn while #149 is stuck).

## Notes

- The PR-body session-narration leak (2026-05-18-wf-author-pr-body-leaks-session-narration.md) is also present on PR #149's body, confirming that issue persists.
- Operator nudge for #149 specifically: leave a PR review comment naming `test_image_build.py` + `test_runtime_dev_local.py` so `wf-feedback` (driven by `pr_review_submitted` webhook) gets a specific directive the next analyzer pass can act on.
- This is the second "analyzer reports happy state when dispatched on a fail signal" learning in three days (sibling: architect's "already in place" on validate-fail deadlock). The pattern is general enough to warrant a rule across all analyzer roles.
