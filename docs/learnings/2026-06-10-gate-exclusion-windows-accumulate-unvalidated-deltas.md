---
date: 2026-06-10
trigger: surprise
status: captured
related: ADR-0087
---

# Learning: gate-exclusion windows accumulate unvalidated deltas — the first post-reinstatement firing covers the whole window

## Trigger
The medicoder chain-smoke gate was excluded from the coordinator's merge gate
while its own CI hang was fixed (the nta pip-backtracking incident). During
that exclusion window, the entire outbox consumer wave (4 PRs rewiring every
chain consumer's emit path) merged without the chain smoke ever executing
against it. The gate's first live firing after reinstatement showed live=0 —
the tag pipeline was dead in compose (emits landing in outbox tables nothing
drained) — and the failure was initially mis-attributed to the PR that
happened to trigger the firing (#1304) and then to a known fixture-fidelity
item, before evidence (live=0 vs the fidelity item's live>0 signature)
located it in the wave that merged under the window.

## Observation
A scoped, expiring gate exclusion is correct crisis discipline (the
alternative is a frozen merge queue behind a gate that cannot pass). But
every merge during the window ships unvalidated against the excluded gate,
and the suspect set for the first post-reinstatement failure is the WHOLE
window — not the PR that triggered the firing. Mis-attributing to the
trigger PR is the natural error: it is the visible proximate event.

## Generalization
Exclusion windows convert a gate from continuous validation to batch
validation without anyone deciding that. The batch boundary (reinstatement)
needs explicit handling: the first firing validates the accumulated delta,
and its failure indicts the window's merge set.

## Proposed rule
Every gate exclusion records: (a) its expiry condition, (b) the list of PRs
merged under it. On reinstatement, the first firing is treated as validating
that list; on failure, triage starts from the window's merge set, newest
architectural change first — not from the triggering PR.

## Proposed remediation
Coordinator-side: the exclusion audit event carries the window's merge list
(it already logs exclusions per the 2026-06-10 ruling); the reinstatement
event names the covering run. Captured in the medicoder coordinator's gate
discipline as an addendum the same day; this learning generalizes it for
other coordinators/repos.

## Notes
Secondary lesson from the same incident: failure-signature discipline —
live=0 (pipeline dead) and live>0-mismatched (fidelity drift) are different
classes; pattern-matching on a shared headline number (MISSING 48) without
checking the signature dimension caused the second mis-attribution.
