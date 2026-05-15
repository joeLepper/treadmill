# ADR-0038: Ralph-loop deadlock arbitration via wf-architecture-resolve

- **Status:** accepted
- **Date:** 2026-05-15
- **Amends:** ADR-0036 (hands-free review and validation discipline)
- **Related:** ADR-0029 (validator + rule engine, esp. Q29.e cap), ADR-0032 (documentarian + architect roles — role-architect's scope is expanded here), ADR-0037 (sibling amendment closing the author-side loop)

## Context

The post-ADR-0037 ralph loop closed every "failure → remediation" hop. The first end-to-end smoke after the convergence pieces landed (`docs/plans/2026-05-15-convergence-proof-1e.md`, PR #83) hit a different shape entirely: the loop closed on itself in a no-progress state.

Observed cycle on PR #83:

1. wf-author opens trivial PR; `wf-validate.decision=pass`.
2. `wf-review.decision=changes_requested` (model judged the trivial PR as needing changes).
3. `wf-feedback` dispatched; the feedback role examined the diff + the review rationale.
4. `wf-feedback.decision=responded-without-change` ("the requested change has already been applied; PR is already correct").
5. No `pr_synchronize` event fires (no commit to push); mergeability stays `blocked-on-review`; nothing further dispatches.

The two LLM roles disagreed about the same diff. The reviewer's verdict was load-bearing; the feedback role had no authority to override it. **Operator intervention (operator-merge) was the only way out.** That makes the cycle not-hands-free.

This isn't a model-quality issue we can carve as out-of-scope: by definition, any deadlock that requires operator action breaks the property hands-free needs. ADR-0036 §Decision had this carve-out, and the smoke proved it was the wrong scope choice.

## Decision

We expanded `role-architect`'s scope (ADR-0032) so it is also Treadmill's **arbiter for cross-role disagreements that the ralph loop cannot resolve**. The deadlock signal and the dispatch rule:

**Deadlock signal.** A `wf-feedback` step completes with `decision='responded-without-change'` while the underlying task's most-recent `wf-review.decision` is `changes_requested` (i.e., the review still blocks merge). This is the empirical pattern: the executor disagrees with the reviewer and refuses to act. Other cross-role disagreements may surface later; this is the first concrete trigger.

**Dispatch rule.** On the deadlock signal, the consumer dispatches `wf-architecture-resolve` for the same task, carrying the review's rationale + the feedback's rationale as inputs. The architect reads the diff + both narratives and produces an `ArchitectVerdict` per ADR-0032 §Decision:

- `accept-as-is` — the work is fine; reviewer was wrong. Consumer dispatches a synthetic `review.override` event that flips `review_decision='approved'` in the mergeability VIEW projection. The PR moves to `mergeable` and auto-merge follows under ADR-0031.
- `amend` — the work needs changes; the reviewer was right. Consumer dispatches `wf-plan` to author a remediation plan (per ADR-0032's `amend` semantics).
- `supersede` — the disagreement signals a deeper plan issue. Consumer dispatches `wf-doc-amend` against the plan (per ADR-0032's `supersede` semantics).
- `uncertain` — architect cannot decide. After ADR-0032 Q32.e's 5-attempt rework cap, the task surfaces to the operator with `status=blocked-on-architect-cap`. This is the only path to operator intervention, and it is explicit, not silent.

The 5-attempt cap from ADR-0029 Q29.e applies to architect dispatches as well, scoped per task. Dedup namespace: `wf-architecture-resolve:<repo>:deadlock-feedback-run=<wf_feedback_run_id>`.

## Alternatives considered

- **Status quo — operator merges through the deadlock.** Rejected: this is exactly the property we are eliminating.
- **Trust the feedback role as the final say.** Rejected: the executor adjudicating its own work is a known bias vector; the smoke caught the executor saying "no change needed" with no authority to merge.
- **Drop the reviewer's vote when feedback responds-without-change.** Rejected: removes a real safety check (the reviewer might be right and the feedback role might be the one wrong); the disagreement is signal, not noise.
- **Add a third reviewer for tiebreaking.** Rejected: same biases as the first two roles; adding more LLM verdicts doesn't resolve LLM disagreement, just adds more votes.
- **Use role-architect as the arbiter (accepted).** Architect's existing four-verdict shape (`amend` / `supersede` / `accept-as-is` / `uncertain`) maps cleanly onto the disagreement-resolution space, and the role already exists for "structural drift" arbitration. This expands its surface without inventing a new role.

## Consequences

### Good
- The last hands-free open loop closes: cross-role disagreements have a deterministic resolution path.
- Operator surface is explicit and capped, not silent and indefinite.
- Reuses `role-architect` + `wf-architecture-resolve` (ADR-0032) without inventing a new role.

### Bad / trade-offs
- More dispatches per task in disagreement cases; budget pressure on the worker queue.
- The architect's `accept-as-is` flips a load-bearing field (`review_decision`) on the basis of an LLM verdict — this concentrates trust in role-architect's judgment. We mitigate via the 5-attempt cap and the operator surface on `uncertain`.

### Risks
- **Architect deferring to operator too readily.** If `uncertain` becomes the easy out, hands-free regresses. Mitigation: monitor the architect's verdict distribution; tune the prompt or escalate to a stronger model if `uncertain` rate climbs.
- **Loop with three roles disagreeing.** Architect says `amend`, feedback responds-without-change again, architect says `amend` again. Mitigated by the 5-attempt cap.

## Follow-ups

- Add an integration test that exercises the full deadlock → architect → accept-as-is → auto-merge cycle on a fixture.
- **Fifth verdict: `rework`** — captured in conversation 2026-05-15 while implementing this ADR. The four verdicts above resolve disagreement by (a) authoring a remediation plan (`amend`), (b) authoring a superseding ADR (`supersede`), (c) flipping the reviewer's vote (`accept-as-is`), or (d) escalating to operator (`uncertain`). They do not let the architect say "the executor was right that the diff needs work, but the feedback role's first-pass response was inadequate — send it back to feedback with these explicit instructions." That path needs (1) a new verdict literal in `ArchitectVerdict`, (2) a consumer trigger that dispatches `wf-feedback` from `architect.step.completed` when `verdict='rework'`, and (3) a way to surface the architect's `remediation_summary` into the feedback role's prompt. (3) is the real design cost — the candidates are a `task_directive` event the feedback worker queries on startup, a dispatcher-injected synthetic `prior_step`, or a `wf-feedback` context-fetch step. To be authored separately as ADR-0040 to keep this PR scoped to the deadlock-resolution machinery already in flight.
- Decide whether ADR-0032's `wf-architecture-resolve` needs a new `trigger` value to distinguish "deadlock arbitration" from "Class C drift" beyond the `self:wf-feedback-deadlock` trigger string introduced here (probably yes for richer telemetry; today the trigger string is the only signal).

## References

- PR #83 (proof-of-deadlock) — operator-merged 2026-05-15T18:14:40Z; the empirical case this ADR closes.
- ADR-0032 — role-architect + wf-architecture-resolve; this ADR expands its trigger surface.
- ADR-0029 Q29.e — the 5-attempt cap that scopes architect dispatches.
- ADR-0036 — parent; ADR-0037 + this ADR are sibling amendments.
