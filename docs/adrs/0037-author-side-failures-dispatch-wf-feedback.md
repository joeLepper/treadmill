# ADR-0037: Author-side validation failures dispatch wf-feedback

- **Status:** accepted
- **Date:** 2026-05-15
- **Amends:** ADR-0036 (hands-free review and validation discipline)
- **Related:** ADR-0029 (validator + rule engine, esp. Q29.e cap and Q29.f severity), ADR-0036 (parent — single-channel verdict + kind-aware rules)

## Context

ADR-0036 committed us to two axes of gate consistency for hands-free: single-channel verdict and kind-aware rules. While operationalizing it (`docs/plans/2026-05-15-hands-free-convergence.md`), the dispatched author tasks repeatedly hit a third failure mode the parent ADR did not cover: **the author's own pre-push validation can fail, and when it does the task silently stalls.**

Concrete shape, observed across multiple session cascades: `wf-author` runs, the role authors a change, the code disposition runs the task's deterministic validation script per ADR-0029 (the author-side check added as task #121), the script exits non-zero, the disposition emits `step.completed` with `decision='fail'`, and **nothing else happens.** No `wf-feedback` dispatch, no retry, no operator surface — the task ends in a dead state until manual intervention.

This was first captured at `docs/learnings/2026-05-14-author-side-fail-no-remediation.md` after the parser-task stall on 2026-05-14. The convergence cascade made it routine: `66a29882` (rule-manifests author wrote malformed JSON in test fixtures), `82a5731d` (severity-view author produced the wrong file structure), and earlier instances. The system did exactly what task #121 promised — caught the slop pre-push — but stopped at the catch instead of looping back through `wf-feedback`.

Joe's framing 2026-05-15, sharpened the same day: *"If an author fails we don't want the system to just drop the work and give up. We should have a mechanism for retrying. ... We're building a set of concentric ralph loops. If something fails, hand it to a feedback author to remediate."*

The concentric-ralph-loop principle generalizes across the existing layers: `wf-review.changes_requested` → `wf-feedback` (ADR-0029 Q29.e); `wf-validate.fail` → `wf-feedback` (Q29.e + ADR-0036); `pr_synchronize` re-validates against new HEAD (ADR-0014); CI failures → `wf-ci-fix`; merge conflicts → `wf-conflict`; structural drift → `wf-architecture-resolve` (ADR-0032). Each layer's failure routes to a remediation workflow; no layer silently drops work. **Author-side validation is the one remaining layer where the loop is open.** This ADR closes it.

## Decision

We amended ADR-0036 with a third commitment, parallel to the existing two:

**Author-side validation failure dispatches `wf-feedback`.** When a `wf-author` step (or the equivalent code-emitting workflows: `wf-feedback`, `wf-ci-fix`, `wf-conflict`) completes with `decision='fail'` because the task's deterministic validation script returned non-zero pre-push, the consumer dispatches `wf-feedback` for the same task. The dispatch carries the validation failure summary (rationale + log excerpt) as the feedback prompt so the re-authoring agent reacts to the specific failure. The 5-attempt cap from ADR-0029 Q29.e applies across all wf-feedback sources for the same task: author-side fail joins `wf-validate.fail`, `wf-validate.error`, and `wf-review.changes_requested` under the same per-task budget. Beyond the cap, the task surfaces to the operator with `status=blocked-on-feedback-cap`.

Dedup namespace follows the ADR-0026 pattern: `wf-feedback:<repo>:author-fail-run=<wf_author_run_id>`. Different source namespaces prevent multiple feedback sources from colliding on the dedup table for the same task.

## Alternatives considered

- **Status quo — operator manually re-fires** (rejected; this is what we've been doing all session, defeats hands-free).
- **Make author-side validation advisory** (skip the pre-push gate, let CI catch it post-push; rejected because that defeats task #121's whole point — keep authors honest before the work reaches CI, where re-author cycles are slower and more expensive).
- **Dispatch `wf-author` again instead of `wf-feedback`** (rejected because the feedback role exists exactly to read a prior failure and decide what to change; running the author cold loses the failure context).
- **Surface immediately to operator without trying again** (rejected because most author-side fails are recoverable — a typo, a missing import, malformed test fixtures — and hands-free should absorb those without operator labor).

## Consequences

### Good
- Author-side validation produces forward motion instead of a stall, completing the discipline ADR-0036 commits to.
- The dispatch table now has uniform handling: every decision routes somewhere — gates compose without dead ends.
- The 5-attempt cap from ADR-0029 Q29.e prevents runaway retries; operator surface is still well-defined.

### Bad / trade-offs
- One more wf-feedback source means slightly more contention on the dedup table — manageable via the per-source namespace.
- The cap's shared budget means an author who fails its own validation eats into the same budget that wf-validate or wf-review failures would.

### Risks
- **Loop stickiness** — a task where the agent can never write its own validation correctly will burn all 5 attempts and reach the cap. Mitigation: the operator-surfaced state at cap is well-defined; the failure mode is visible, not silent.
- **Feedback payload quality** — wf-feedback's effectiveness depends on the validation failure surfacing enough context for the next author to react. Mitigation: the disposition already captures `log_excerpt` per the 2026-05-15 stdout fix; the feedback prompt carries it verbatim.

## References

- ADR-0036 — parent ADR this amends.
- ADR-0029 Q29.e — 5-attempt cap; Q29.f — severity gating; the wf-validate.fail → wf-feedback path this mirrors.
- ADR-0026 — dispatch-dedup composite key.
- `docs/learnings/2026-05-14-author-side-fail-no-remediation.md` — the original capture.
- `docs/plans/2026-05-15-hands-free-convergence.md` — implementation lands as a new task in this plan.
