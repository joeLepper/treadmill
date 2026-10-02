---
date: 2026-10-01
trigger: pattern
status: captured
related: ADR-0105, ADR-0107
---

# Learning: A cross-model review must judge the kind of artifact it reviews

## Trigger
An ADR-0105 cross-model pass over an ADR returned `block`. The reviewers did not
block the DECISION. They blocked because the ADR did not specify an implementation
mechanism — a detail the ADR deliberately left to its plan. The generic panel rubric
(`tools/model-review-panel/panel.py`) judges every artifact as if it were code.

## Observation
The panel used one rubric for all artifact kinds. A decision record and a code diff
got the same checks. So an ADR that was sound as a decision failed the gate for an
implementation gap that does not belong in an ADR.

## Generalization
We tend to reuse one review rubric across artifact kinds. A review built for code
over-fires on a decision record, because "mechanism unspecified" is a defect in code
but is correct in an ADR or a plan. A review must know what kind of artifact it reads.

## Proposed rule
A cross-model review of an ADR, a plan, or a design note must judge the DECISION, not
the implementation mechanism. A missing or under-specified mechanism is non-blocking
for such a document. Block only on a decision defect: a contradiction, an unfalsifiable
claim, a false factual claim, a cited-ADR-invariant violation, or a wrong decision.

## Proposed remediation
`panel.py` now takes `--kind code|diff|adr|plan|design` and appends a decision addendum
for the decision kinds; it infers the kind from the artifact path when the flag is
absent. `review-pr.sh` sets the kind. `panel_test.py` covers the behavior. Use the
`adr`/`plan`/`design` kind for every ADR-0105 pass.

## Notes
Shipped with the panel change on 2026-10-01. See [[feedback_no_fabricated_precision_in_adrs]]
— the two pull the same way: an ADR states a decision and its assumptions, not a proven
mechanism, so a review must not demand the mechanism.
