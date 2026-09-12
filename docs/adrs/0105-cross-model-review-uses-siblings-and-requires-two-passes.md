# ADR-0105: Cross-model review uses the sibling reviewers and requires two independent passes

- **Status:** accepted (2026-09-11; operator-directed; evidenced by the ADR-0104 review round where two cross-model passes found disjoint defects)
- **Date:** 2026-09-11
- **Related:** ADR-0104 (Gerald, open-weight sibling), ADR-0102 (Fran, GPT sibling)

## Context

The fleet's review discipline (the `decide`, `plan`, and `adversarial-review`
skills) calls for an independent adversarial review before an ADR settles, a plan
submits, or a change merges. Two facts now change how that review should be
sourced:

1. We have genuine non-Anthropic reviewers on the bus: Fran (GPT/`gpt-6-astra`,
   ADR-0102) and Gerald (open-weight models, ADR-0104). Both catch failure modes
   the Claude-Code siblings share, because they are different model families in a
   different harness.
2. On this very changeset, TWO independent cross-model passes each found real,
   distinct defects: Fran (cross-model) caught a fail-open denylist, an API key
   leaking into tmux scrollback, and a cross-thread bootstrap bug; Ernie (sibling)
   independently converged on the same open-weight-enforcement gap. One pass would
   have missed some of these.

## Decision

We decided two things about how cross-model review is sourced (one policy, stated
as two rules of the same decision — how the fleet obtains cross-model review):

1. **Cross-model adversarial reviews route to the sibling reviewers — Gerald
   (open-weight) and Fran (GPT) — over the bus, not to Tapestry evaluator agents.**
   Gerald is the default adversarial reviewer.
2. **A high-stakes change gets TWO independent cross-model passes**, from two
   different model families (e.g. Gerald + Fran), in addition to a same-family
   sibling review. "High-stakes" = an ADR, a plan submission, or a
   security/blast-radius change. Routine changes still get at least one.

This is contingent on the open-weight reviewer being capable enough to give a real
review; where a model returns weak/empty output, fall back to another Go model or
to Fran, and record which reviewer actually ran.

## Alternatives considered

- **Incumbent: Tapestry evaluator agents for cross-model review.** They run inside
  the same runtime and, for cross-family diversity, are a model swap within one
  harness. **Why insufficient:** genuine cross-harness/cross-family review needs an
  agent that fails and succeeds differently (ADR-0102's premise); a Tapestry
  evaluator does not provide that, and the sibling reviewers now do.
- **Single cross-model pass.** **Why rejected:** this changeset is the
  counter-example — two passes found disjoint real defects; one would have shipped
  some.
- **Require two passes on everything.** **Why rejected:** cost. Routine changes do
  not warrant two cross-model reviews; reserve the second pass for high-stakes.

## Consequences

### Good
- Real cross-family scrutiny; two passes demonstrably catch disjoint defects.
- Uses live fleet capability (the bus siblings) instead of an in-runtime swap.

### Bad / trade-offs
- Latency and token cost: two cross-model passes plus a sibling review.
- Depends on reviewer-model capability; a weak Go model can produce a low-value
  pass that must be re-routed.

### Risks
- A cross-model reviewer under a shared identity cannot click GitHub Approve; the
  co-sign is by relay (ADR-0084 / exec-in-charge), same as sibling review.
- **Falsifier:** a high-stakes change (ADR, plan submission, or security/blast-
  radius change) merges with fewer than two independent cross-model co-signs on
  the record, or with a cross-model review sourced from a Tapestry evaluator rather
  than a sibling reviewer. Either shows this decision is not being honoured.

## References

- ADR-0104 (Gerald), ADR-0102 (Fran), ADR-0084 (relay co-sign under shared identity).
- This changeset's review round: Fran + Ernie found disjoint real defects.
