---
date: 2026-05-20
trigger: surprise
status: captured
related: ADR-0004, ADR-0030, ADR-0048
---

# Learning: design diagrams can lag the merged code — verify against code, not the diagram

## Trigger

Joe asked me to *prove* a claim about how the system behaves rather than assert
it, specifically by comparing the diagrams drawn that day against the actual
code via subagents. The diagrams in question were PR #196's
`docs/diagrams/task-flow-wf-feedback.md` and `task-flow-dead-ends.md`, authored
2026-05-19. Three subagents cross-checked their claims against the code.

The surprise: the diagrams — the *newest* design artifact, still an open PR —
were substantially behind the merged code. Their "proposed / today-broken /
new" annotations described work that had already shipped:

- `maybe_dispatch_architect_on_feedback_validation_fail` was described as "new
  trigger needed" — it already existed (ADR-0048 / PR #198), wired and tested.
- author-no-diff "today routes to wf-feedback" — already routed to
  wf-architecture-resolve (PR #187).
- `supersede` marked "New" — already implemented (PR #181).
- `uncertain` "being removed" — already removed (PR #179).
- Every "ADR-0049" reference — the decision merged as **ADR-0048** (renumbered).

My own reading of the code (which underpinned a board cleanup) was confirmed
correct; it was the diagram that was stale.

## Observation

ADR-0004 and ADR-0030 establish diagrams as the *contract of intent* — the
authority an implementation is checked against. That framing quietly assumes the
diagram is current. But a diagram authored as a *proposal* (describing a future
state) and merged/looked-at later, after the proposal shipped, inverts into a
*misleading* artifact: a reader trusts it as current and concludes work is
undone when it is done. The diagram's own "proposed vs shipped" annotations are
exactly the part that rots fastest, because nothing forces them to be updated
when the corresponding PR merges.

The code does not have this failure mode: it is, tautologically, what the system
does. So when a diagram and the code disagree, the code wins — even when the
diagram is newer.

## Generalization

Any durable design artifact that mixes "current state" with "proposed state"
(diagrams, ADRs in `proposed` status, plan docs, dead-end catalogs) will drift
into misleading-current as its proposals ship, unless something reconciles it at
merge time. The drift is invisible because the artifact still reads as
authoritative. This is adjacent to the dual-encoding-projections failure (two
sources encoding one concept; a reader trusting the wrong one) but at the
docs↔code boundary rather than column↔column.

Practical consequence for how I work: when asked whether the system does X,
**verify against the code**, and treat even a freshly-authored diagram as a
claim to check, not a source of truth. Conversely, when a proposal-shaped
diagram ships, it owes a reconciliation pass flipping proposed→shipped (with the
PR number) — otherwise the next reader is misled.

## Proposed rule

A diagram or doc that annotates items as "proposed / new / today-broken" MUST be
reconciled when the corresponding change merges: flip the annotation to shipped
with the PR number, or delete it. Candidate enforcement:
- **Deterministic** — a freshness marker (`verified-against: <sha>` front-matter,
  or a "Status as of <date>" line) plus a CI lint that flags diagrams whose
  marker is older than the newest commit touching the code paths they name.
- **LLM-judge** — during review of a PR that closes a cataloged item, a reviewer
  check: "does any diagram still describe this as proposed/broken?"
- The cheapest first move is the freshness marker + the reconciliation habit;
  the lint is the durable backstop.

## Proposed remediation

When the rule is violated (a diagram describes shipped work as proposed):
- Reconcile the diagram in the same change that surfaced the drift (done here:
  both diagrams updated to ADR-0048 / shipped-with-PR-numbers as part of the
  2026-05-20 batch).
- Prefer referencing the PR that shipped each item inline, so the annotation
  carries its own provenance and the next drift is cheap to spot.

## Notes

- This learning is itself the reconciliation trigger: PR #196 began as a
  proposal-shaped diagram and is being merged as an already-reconciled one.
- Relationship to ADR-0030: this doesn't weaken "diagrams are the contract of
  intent" — it adds the missing clause that the contract must be kept current,
  and that *code* is the tiebreaker when they disagree.
- Process note worth keeping: Joe's instinct to demand proof-by-verification
  (subagents comparing the artifact to code) is what surfaced this. Confident
  claims about system behavior should be grounded in a code check, not asserted.
