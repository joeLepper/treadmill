# ADR-0047: Replace LLM non-determinism with deterministic state in the auto-merge path

- **Status:** accepted
- **Date:** 2026-05-18
- **Related:** ADR-0026 (workflow-dispatch dedup), ADR-0027 (structured JSON for review output), ADR-0029 (ralph-loop validation runner), ADR-0031 (auto-merge on mergeable), ADR-0036 (hands-free review and validation discipline), ADR-0037 (author-side failures dispatch wf-feedback), ADR-0038 (deadlock arbitration), ADR-0040 (architect tunes validator), ADR-0042 (validate.override channel), ADR-0046 (operator task retry CLI)

## Context

Treadmill's auto-merge pipeline is a graph of workflow steps: `wf-author` → `wf-validate` + `wf-review` → cooling-off → merge. Each gate produces a verdict. The verdicts feed a projection (`task_mergeability` VIEW) which drives the auto-merge predicate. The system is designed to run hands-free.

In practice during the 2026-05-18 push to validate hands-free reliability, two failure-mode families repeatedly stalled the pipeline:

1. **LLM-judge spuriousness.** Validator rules that delegate the verdict to an LLM (`validation-script-executed`, `surface-changes-have-doc-updates`, `purpose-articulated-in-collapse-proposal`, others) fired `fail` on PRs whose work was demonstrably correct. The judge was reading worker prose for evidence-of-execution, evidence-of-doc-update, evidence-of-purpose-articulation — fails were often a function of how well the worker articulated its work, not whether the work was done. Architect arbitration via `accept-as-is` (ADR-0042) eventually overrode each one, at the cost of 5-7 deadlock cycles per PR (~30 minutes of worker time, plus a window where the cap could fire and orphan the task).

2. **Workflow-event firing surfaces.** The auto-merge predicate was wired to fire only on step.completed for a named allowlist of workflows (`{wf-validate, wf-review}`). When ADR-0042's architect-override path landed, the predicate didn't fire on the architect's step.completed — the override events were emitted, the VIEW projected `mergeable`, but the cooling-off deadline was never set. PR #154 added `wf-architecture-resolve` to the set, but the architectural fragility remained: every new override channel would require the same fix.

Both failures share a root: **the pipeline uses LLM judgments and named-workflow gates in places where deterministic state is available.**

## Decision

We adopt the principle: **in the auto-merge path, prefer deterministic state-based triggers and gates over LLM judgments and named-workflow allowlists.**

This is a directional decision, not a single PR. It governs how we evaluate future changes to the gate/trigger surface. The principle has been instantiated in this session via the following changes:

### Already shipped this session

1. **#150 — Sharpen role-ci-analyzer and role-code-author against "already in place" loops.** Adds explicit forbid-lists for prose patterns that frustrate the recovery loop (e.g., role-ci-analyzer claiming "task is already complete" when CI is red; role-code-author claiming "implementation is already in place" when CI named files outside its task scope). Replaces "trust the LLM to do the right thing" with "trust + verify via forbidden-output pattern matching."

2. **#152 — Wire wf-feedback dispatch on bare step.failed.** Previously ADR-0037 fired only on `step.completed` with `decision='fail'`. Silent worker death (crash, OOM) produced bare `step.failed` with no decision payload, leaving the task zombied with no automatic retry. The fix adds a deterministic trigger: any wf-author step.failed dispatches wf-feedback under the same dedup namespace and cap.

3. **#154 — Auto-merge predicate fires on wf-architecture-resolve completion.** When the architect verdicts `accept-as-is` and emits `review.override` / `validate.override` events (ADR-0042), the predicate needed to fire to set the cooling-off deadline. Adding the workflow to the allowlist (this PR) closed the immediate gap; #163 (below) closes the architectural fragility.

4. **#155 — Learning: auto-merge predicate missed wf-architecture-resolve completion.** Captures the structural lesson: override projections need both visibility (PR #142's flush-before-read) AND firing (this ADR's deterministic trigger surface).

5. **#158 — Demote `validation-script-executed` LLM-judge to severity=warning.** Replaces a redundant LLM check with the existing deterministic sibling: `pytest-collect-pass` already proves validation ran. Keeping the LLM-judge at blocking forced architect arbitration cycles for every PR whose worker prose didn't satisfy the judge.

6. **#161 — Honor `--dev` fast-path in dev_local (not just fully_local).** The `treadmill submit` CLI's intent-only path created inert plans (status=drafting) in dev_local because the deterministic mode predicate wrongly gated on `is_fully_local`. The wf-plan PR-merge gate the flag skips is governance, not AWS substrate; widening the predicate is correct.

7. **#162 — role-reviewer anti-spurious-changes_requested forbid.** Extends the prompt-forbid pattern (PR #150) to the reviewer role. Spurious `changes_requested` cycled trivial PRs through 2x review-feedback loops before architect override. Forbid-list names non-grounds: body-quality, title-length, missing forward-looking tests, slight uncertainty. Unconditional-approve rule for trivial scoped changes.

8. **#163 — Auto-merge predicate fires on any step.completed (drop workflow-set gate).** Removes `_AUTO_MERGE_TRIGGER_WORKFLOWS`. The predicate's own mergeability checks short-circuit cleanly; opening the firing surface closes the gate-set bookkeeping burden for future override channels.

### Operator/system flanks (paired but not auto-merge-path)

- **#148 — Heartbeat refactor (pulse file → log mtime).** Replaces the redundant pulse-file with the existing log-mtime substrate. The 2026-05-17 learning ("don't reinvent logging") is the same anti-pattern reflex this ADR formalizes: prefer the deterministic substrate already in place.

- **ADR-0046 — Operator task retry CLI (proposed).** First-class surface for the operator's "synthesize a step.completed event" workaround. Replaces an ad-hoc deterministic action (raw SQL or AWS CLI) with an audit-logged, gated CLI command.

## Principle: when to replace, when to keep

LLM judgment is load-bearing for genuinely-semantic decisions: did the diff implement the task's *intent*; is the architect's deadlock arbitration sound; is this code change conformant to a cited diagram. We keep LLM judgment in those places.

We **replace** LLM judgment when:

1. A **deterministic sibling check** already produces the same signal at higher reliability. (`validation-script-executed` vs `pytest-collect-pass`.)
2. The judgment is **about the worker's prose**, not the work product. (Prose-evidence-of-execution; prose-evidence-of-doc-update for purely-internal changes.)
3. The judgment fires repeatedly on the **same task** without forward motion — i.e., it's a known-spurious-fail-driver. (Validator rules that consistently get architect-overridden.)
4. The judgment **gates a structural decision** that would be more cleanly modeled by state. (Named-workflow allowlists for the auto-merge trigger surface; named-substrate gates for `--dev` flag.)

We **keep** LLM judgment when:

1. The decision is **per-PR-semantic**, not boilerplate-shaped. (Architect arbitration; reviewer approval on substantive code changes.)
2. No deterministic check exists or could be cheaply added. (Code-author choosing how to structure a fix.)
3. The cost of being wrong is **bounded by an existing gate**. (Code-author's commit gets validate-checked deterministically.)

## Alternatives considered

- **Full event-driven `task_mergeability.changed` projection.** The cleanest long-term design is a projection-change-event that fires auto-merge whenever the VIEW transitions to `mergeable` for a task. We chose the smaller iteration (#163) because the predicate's internal checks already short-circuit, and the projection-change-event mechanism would need a new schema primitive (PostgreSQL has LISTEN/NOTIFY but no native VIEW-change triggers, so this would be an application-level concern). Filed as a future refinement.

- **Replace all LLM-judges with deterministic equivalents.** Rejected as over-aggressive — some judges (the architect, the reviewer on substantive work) are doing real per-PR semantic work. The principle is targeted: replace **redundant** or **prose-judging** LLM gates, not all of them.

- **Lower `FEEDBACK_MAX_ATTEMPTS` to make spurious fails fail faster.** Rejected — the cap exists to bound the loop, not to characterize the failure. Lowering it would also cap legitimate recovery cycles.

- **Bump role-reviewer / role-code-author to sonnet.** Considered but rejected for cost. Haiku follows explicit forbid-lists well enough when the prompt is unambiguous (see PR #162's structure). The cost differential is significant and the forbid-list approach has proven sufficient.

## Consequences

### Good

- Future override channels (e.g., a hypothetical `ci.override`) fire auto-merge naturally without trigger-set updates.
- LLM-judge demotions can proceed PR-by-PR as spurious patterns are observed, with a clear principle to cite.
- Operator nudges (synthetic events, manual SQL) have a first-class surface coming via ADR-0046 — fewer ad-hoc interventions.
- The pipeline's hands-free property is more robust against future prompt iterations: changes to LLM behavior don't quietly break gates.

### Bad / trade-offs

- Removing LLM gates loses some safety margin. The principle's "when to replace, when to keep" section is the discipline that prevents over-correction.
- The workflow-set-removal (#163) costs one extra `task_mergeability` VIEW read per step.completed. Acceptable; the VIEW reads are fast and the predicate fast-bails on non-relevant states.

### Risks

- A real defect that the demoted LLM-judge would have caught now lands. Mitigation: the architect arbitration path still exists for real concerns; severity-warning rules still surface their verdicts in PR review for operators.
- Operator team grows confused about which gates are "real" vs "informational." Mitigation: this ADR documents the principle, and per-rule comments cite the principle when severity is downgraded.

## Notes

- This ADR is descriptive of changes shipped 2026-05-18 plus a forward-looking principle. The eight code PRs cited above implement the decision; this ADR is the durable record of why they're a coherent set rather than a grab-bag of papercut fixes.
- The "real-task" validation will be the next pass per Joe's directive: five tasks from half-finished plans, watched end-to-end for hands-free flow, with any new failure modes captured as durable changes under this same principle.
