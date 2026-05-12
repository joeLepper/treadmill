---
date: 2026-05-11
trigger: correction
status: captured
related: ADR-0010, ADR-0011, plan:2026-05-08-minimum-runnable-treadmill
---

# Learning: When bunkhouse has already solved a shape question, default to its answer

## Trigger

Discussing Week 3 workflow design, the orchestrator proposed per-workflow typed step outputs — `ReviewStepOutput`, `ValidateStepOutput`, `FeedbackStepOutput`, etc., with field sets tailored to each workflow. The user pushed back: *"Just like with bunkhouse I don't think that we want workflow or even role-specific output shapes. Rather we want a standard output shape that all roles and workflows conform to. … having a standard format should pay dividends as we evolve the system, and allow us to let users update things in the ui if we so choose."*

The orchestrator had been reasoning *forward* from the technical question "what's the most type-safe way to validate step outputs at the boundary?" The answer is *discriminated unions*, and that's where the proposal landed. The user reframed: we have a working answer from bunkhouse already; defaulting to it is cheaper than re-deriving.

## Observation

This is a calibration failure adjacent to but distinct from `2026-05-08-commodity-vs-architectural-decision-weight.md`. That learning is about *which decisions to defer*; this one is about *which decisions are already settled by precedent we should trust*.

The orchestrator's bias is to optimize for the *current* code's type-safety contract. Per-workflow typed outputs give compile-time guarantees that `payload["pr_number"]` is an int specifically when the step is `wf-author`. That's a real win in a small static system.

But Treadmill is not a small static system. It's a system that:

- Renders step outputs in a UI (Phase 4+) where a single render path that handles every workflow is dramatically cheaper than per-workflow render code.
- Evolves new workflows over time (wf-review variants, wf-validate kinds, future roles we haven't named). A uniform envelope lets each new workflow exist without a schema migration on the consumer side.
- Lets operators inspect / search / aggregate across workflows. A uniform `summary` field that every step populates means one query, not one per workflow kind.

Bunkhouse went through the per-role schema phase and reached the uniform envelope. The orchestrator should have asked the precedent question *before* drawing up the discriminated-union design — *did bunkhouse face this already, and how did they land?*

## Generalization

When the orchestrator's analysis lands on a shape decision (output schemas, naming conventions, state-machine modeling, message envelope structure), the precedent question is the first check:

> Did bunkhouse land on a shape for this? If yes, the default is "match it"; the burden is on deviation, not adoption.

This is a different question from "is this a commodity?" Commodity choices need precedent for *speed* (don't re-derive). Shape choices need precedent for *coherence* (the system evolves better when its parts agree). Same source-of-truth (bunkhouse), different motivations.

Concrete application to Treadmill's step output shape:

- **Envelope** (typed Pydantic): `summary: str`, `decision: str` (free string the consumer matches on), `artifacts: list[Artifact]`, `payload: dict[str, Any]`, `metadata: Metadata`. Same for every role.
- **Per-workflow content** lives in `payload`; consumers know by convention what to expect there. The convention is documented, not statically enforced at the publish boundary.
- **The A.4 `AuthorStepOutput` from the closure plan** demotes from "the canonical type of `step.output` for wf-author" to "a documented convention for what `payload` carries when `workflow_id='wf-author'`."

The principle is: **the boundary is uniform; per-workflow specificity lives in convention, not in types.** Same shape ADR-0011 favors for the events table (uniform envelope, polymorphic payload).

## Proposed rule

A candidate — second instance of "match-bunkhouse-precedent on architectural shape." First instance was implicit in the project's framing from day one (bunkhouse is the technical-decisions starting point). This is the first time the orchestrator visibly *missed* the precedent question and was corrected.

If a third instance arrives, the rule shape is something like:

> *Before authoring a shape decision (schema, envelope, state machine, naming convention), check what bunkhouse landed on. Adopt-by-default; deviate only with a documented reason. The orchestrator's instinct to optimize for current-state type-safety is a frequent failure mode here.*

This pairs with `2026-05-08-commodity-vs-architectural-decision-weight.md` — both are about asking *"is this question already answered?"* before deriving from first principles.

## Proposed remediation

None yet — wait for the rule. But the practical application is immediate: revise the Week 3 design to use a uniform step-output envelope. Demote per-workflow output types into payload conventions. Reconcile A.4 in the closure plan accordingly.

## Notes

The auto-capture hook caught "i don't think" again in the user message that surfaced this. The trigger was the actual correction (not a false-positive instance like `2026-05-08-per-role-images-collapse-attempt`).

The cost of this miss in the closure plan was small — A.4 only lightly typed `StepCompleted.output`, and demoting it is straightforward. The cost in Week 3 would have been larger had the orchestrator pushed forward with discriminated-union outputs across all seven workflows.
