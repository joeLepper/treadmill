---
date: 2026-09-14
trigger: surprise
status: captured
related: ADR-0113
---

# Learning: a green DB foil can be VACUOUS if its fixture seeds a state production never has

## Trigger
Building the ADR-0113 CI-leg poll-ingest, my DB foil asserted the `ci_observer`
attributed a `task.ci_result` and passed GREEN. Ernie co-signed on that green (his
honest boundary — the test is `@integration` and skips on his box). Building the
poller (slice 3), I traced attribution and found the foil's fixture seeded
`task_prs.head_sha = head_sha`. On a REAL webhookless repo NOTHING populates that
column (the seam's head_sha writer fires only on `pr_opened`/`pr_synchronize` WEBHOOK
events, which never arrive; the coordinator's `task_prs` registration carries no head).
So on the actual dogfood repo the CI leg would have attributed ZERO ci_results.

## Observation
The foil was green and non-vacuous WRT the property it named (observer derives the
ci_result), but its FIXTURE encoded a precondition (head_sha already set) that the
production path never establishes. The green proved the code works GIVEN a state that
cannot occur — a masked gap, not a real pass. Reddening required seeding the fixture
to the real poll-repo state (head_sha NULL), which then forced the actual fix (the
endpoint writes head_sha itself).

## Generalization
A DB/integration foil has two failure modes, not one. The usual one — the assertion
is vacuous — we guard with red-then-green. The second — the FIXTURE is unfaithful —
red-then-green does NOT catch, because both the red and the green run against the same
wrong precondition. When a foil seeds a row a production writer would have written,
ask "what writes this column on the real path, and does my scenario reproduce that?"
before trusting the green. A reviewer can ask this WITHOUT the integration DB — it is a
wiring question about the fixture, not a runtime check.

## Proposed rule
When a DB foil seeds a column/row that a production code path is responsible for
populating, either (a) drive the production writer in the test instead of seeding the
value, or (b) seed the value the production path leaves it at (often NULL/absent) and
assert the code-under-test establishes the rest. Never seed a downstream-populated
value to a convenient state just to make the assertion reachable.

## Proposed remediation
Reviewer checklist addition: for each fixture INSERT in a DB foil, name the production
writer of every non-trivial column; if none exists on the path under test, the fixture
is unfaithful. Both author and co-signer apply it.

## Notes
Caught by me mid-build; Ernie recorded the reciprocal reviewer lesson ("verify a DB
foil's fixture reflects production state, not just that it's green"). Fix + red-then-
green (disable the writer → both CI foils red) in ADR-0113 slice 3. Related to
[[feedback_foil_every_input_of_a_derived_property]] and
[[feedback_verify_wiring_not_value_exists]].
