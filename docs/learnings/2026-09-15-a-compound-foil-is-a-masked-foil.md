---
date: 2026-09-15
trigger: pattern
status: captured
related: ADR-0102, feedback_foil_every_input_of_a_derived_property, adversarial-review
---

# Learning: a compound foil is a masked foil — one invariant per negative case

## Trigger
During Fran's (the Codex sibling's) cross-family adversarial review of the run_shape v1
seam contract (2026-09-15), the SAME defect class surfaced THREE times across review
rounds. Each time, an enforcement fixture's negative ("reject") case violated MORE THAN ONE
invariant at once:
- `value_leak` put a bad value in BOTH `unit` AND `counts.edit` — Fran ran a validator that
  never checked `unit` but rejected non-integer counts: it still rejected `value_leak`,
  yet accepted `unit:"PRIVATE_PROMPT"` alone (a real content leak).
- `integer_overflow` had a `counts` sum that mismatched `total` AND exceeded the ceiling —
  a validator with NO bound checks rejected it (on the sum) yet accepted `rework=1000001`.

## Observation
A negative fixture case that trips several invariants at once is satisfied by an
implementation that enforces only ONE of them. The suite goes green while the specific
invariant the case was meant to guard is entirely unenforced. The green is a lie about
coverage, not about the code. A local SHA/keyset pin does not catch this — the fixture is
self-consistently wrong.

## Generalization
An enforcement fixture proves an invariant only if its negative case is INDEPENDENTLY
invalid on that ONE invariant — every other field valid. Otherwise a passing case cannot
distinguish "the target rule is enforced" from "some OTHER co-violated rule fired first."
This is the fixture-design twin of [[feedback_foil_every_input_of_a_derived_property]] (foil
each input separately) and the adversarial-review rule to prove the check can fail for the
RIGHT reason (break exactly the guard under test, confirm THAT case flips).

## Proposed rule
One invariant per negative fixture case: the case is invalid on exactly the field/rule
under test, with every other field valid and a matching positive boundary case. A negative
case that violates two or more rules at once is a MASKED foil and does not prove either.

## Proposed remediation
- Author: when writing an enforcement/reject fixture, split any multi-violation case into
  single-vector cases (e.g. `value_leak_unit_only`, `value_leak_counts_object`,
  `..._string`; `rework_over_max`, `duration_over_max`, `negative_value`) + a boundary
  ACCEPT case.
- Reviewer check (cheap + decisive): run the suite against a mutant validator with the
  target check REMOVED; if every negative case still rejects, at least one case is masked —
  it rejected for the wrong reason. This is the executable form of the rule.

## Notes
Caught live and repeatedly by an INDEPENDENT cross-family reviewer running executable
countermodels (see [[feedback_cross_family_pass_catches_evaluator_blindspot]]); the
same-family authors had written self-consistent compound cases and did not see the gap.
By round 3 the consumer author (Donna) had adopted a proactive single-vector audit of all
her cases rather than being fed the defect round by round — the lesson propagating before
capture.
