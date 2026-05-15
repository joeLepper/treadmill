# ADR-0039: Validator-infrastructure errors do not gate merge

- **Status:** accepted
- **Date:** 2026-05-15
- **Amends:** ADR-0036 (hands-free review and validation discipline)
- **Related:** ADR-0006 (rules + remediations primitive), ADR-0013 (mergeability VIEW), ADR-0020 (observability), ADR-0029 (validator + rule engine, esp. Q29.f severity gating), ADR-0036 (parent — single-channel verdict + kind-aware rules), ADR-0037 (sibling: author-side fail → wf-feedback), ADR-0038 (sibling: cross-role deadlock → wf-architecture-resolve)

## Context

ADR-0036 (severity-aware + kind-aware) plus ADR-0037 (author-side feedback) plus ADR-0038 (deadlock arbitration) closed the principled failure paths through the gates. The first post-convergence smoke (PR #84, 2026-05-15) still required operator-merge — but for a different reason than before. The three blocking failures that gated were:

1. `surface-changes-have-doc-updates` LLM-judge — emitted `{result: 'uncertain', ...}` instead of the schema's required `{verdict, rationale}` field. Stored as `verdict='error'` with rationale "JSON parse failed."
2. `implementation-conforms` LLM-judge — Claude timed out after 60s. Stored as `verdict='error'` with rationale "CodeAuthorError: Claude timed out."
3. `pytest-collect-pass` deterministic — pytest collection returned non-zero in the worker environment, despite the same `uv run pytest --collect-only` succeeding locally (863 tests collected). Stored as `verdict='fail'`.

(3) is ambiguous — could be the code is broken or the rule's environment differs — but (1) and (2) are unambiguously **the validator failing to evaluate**, not the code failing the check. The aggregate treated all three as blocking and required operator-merge to ship a change that was, by every actual judge that *did* run, correct.

Joe's framing 2026-05-15: *"If the code under validation throws an error that's a failure. If the validator itself errors that errored. We should have insight via o11y but allow the changeset to proceed."*

The taxonomy `pass` / `fail` / `error` already distinguishes "judge ran and said yes / no" from "judge couldn't run." The mergeability aggregate just wasn't honoring that distinction.

## Decision

We decided: **`verdict='error'` from a validator check does not gate merge.** Aggregation rules become:

- `pass` — judge ran, said the check is satisfied. No impact on the aggregate.
- `fail` — judge ran, said the check is not satisfied. **Gates merge** if `severity='blocking'`.
- `error` — judge did not produce a verdict (parse failure, timeout, environment broken, subprocess crash, etc.). **Does not gate merge regardless of severity.** Surfaced via ADR-0020 observability so operators can spot validator-quality drift and remediate the rule infrastructure.

ADR-0029 Q29.f's severity axis continues to gate; this ADR narrows its predicate from "any non-pass" to "fail specifically." The shape stays two-axis: severity decides whether a *substantive* failure blocks; verdict decides whether the rule produced a substantive result at all.

Implementation: the per-check aggregate in the wf-validate disposition and the `validate.decision` projection in the `task_mergeability` VIEW filter on `(verdict='fail' AND severity='blocking')` rather than `(verdict IN ('fail','error') AND severity='blocking')`. Each `verdict='error'` is logged with `rule_id`, `failure_reason` (the rationale), and `severity` so the observability stack can build a "rule infrastructure health" dashboard.

## Alternatives considered

- **Status quo: errors gate merge.** Rejected — required operator-merge to ship a change every judge that *did* run agreed was correct; defeats hands-free.
- **Introduce a fourth verdict (`skipped`) for infrastructure failure.** Considered, rejected: the existing `error` verdict already means "rule didn't produce a verdict." Adding `skipped` would split a synonymous concept across two values and create a question of when each applies. The change is policy (how the aggregate treats `error`), not taxonomy.
- **Make errored rules `severity=warning`-equivalent.** Rejected — it conflates "rule produced a non-blocking signal" with "rule produced no signal." Operators reading the PR-comment surface need to know which is which: a warning fail means "we ran the check; you might want to address this"; an error means "we couldn't run the check; the rule is broken."
- **Retry-on-error in the validator runtime.** Considered, complementary but not load-bearing: useful for transient failures (timeout, rate limit), worthless for systematic ones (wrong schema). Worth implementing as a follow-up but does not change the aggregation policy.

## Consequences

### Good
- Trivial code-touching PRs (like PR #84) converge through the gates without operator intervention when the rule infrastructure is flaky.
- The taxonomy clarifies operator triage: `fail` → "review the diff against the rule"; `error` → "fix the validator."
- Plays well with ADR-0036's severity axis and ADR-0029 Q29.f: severity decides *blocking-vs-not* for substantive results; verdict decides *substantive-vs-not* up front.

### Bad / trade-offs
- A rule that *always errors* (e.g., a brittle LLM-judge prompt) effectively becomes a no-op. The observability surface is the safety net: operators must watch the error-rate dashboard.
- Some judges might emit borderline-malformed output that *could* be interpreted as `fail`; we trade that interpretation for `error` and proceed. Trust the JSON envelope per ADR-0027 — if the model can't produce the schema, the rule didn't run.

### Risks
- **Silent rule rot.** A rule errors for weeks, no one notices, real failures slip through. Mitigation: ADR-0020 dashboard for `error` rate per rule_id; alert on sustained > N% over rolling window. Build this when the o11y stack lands.
- **Pytest-collect-style ambiguity.** A deterministic check that returns non-zero is `fail` today, not `error`. If the failure is environmental (worker has wrong cwd) it gates merge even though we'd want it not to. Mitigation: rule scripts should distinguish "script ran, check failed" (exit 1) from "script couldn't run" (exit 2 or another sentinel) so the runtime maps accordingly. Follow-up task.

## Follow-ups

- Implementation task: change the wf-validate aggregate + mergeability VIEW SQL to ignore `verdict='error'`.
- ADR-0020 dashboard: per-rule error rate, surface alerts when validator-infrastructure quality drops.
- Rule-script convention: exit-2 (or similar sentinel) for "environment broken, rule didn't run" so deterministic rules can also produce `error` and benefit from this policy.
- Optional retry-on-error in `validation_runtime.run_llm_judge` for transient failures; one attempt with stricter prompt before falling through to `error`.

## References

- PR #84 — concrete case this ADR closes: ADR-0037 implementation, every substantive judge that ran agreed; only LLM-judge schema mismatch + LLM timeout + pytest-collect environment issue gated merge.
- Joe's framing 2026-05-15: *"If the validator itself errors that errored. We should have insight via o11y but allow the changeset to proceed."*
