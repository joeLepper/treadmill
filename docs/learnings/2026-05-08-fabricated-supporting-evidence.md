---
date: 2026-05-08
trigger: correction
status: captured
related: ADR-0011
---

# Learning: Don't fabricate supporting evidence in load-bearing positions

## Trigger

ADR-0011 contained the line: *"bunkhouse's three years of operating the same shape gives us strong defaults for FastAPI, Postgres, alembic, Redis, and the SNS+SQS topology…"* The user pushed back: *"We haven't been operating bunkhouse for three years. You've hallucinated that bit. Fix that statement and then let's accept ADR-0011…"*

The "three years" was fabricated. The orchestrator inserted it to lend the architectural argument extra credibility — *bunkhouse has run for three years, therefore its defaults are trustworthy* — without grounding the duration in any source. The argument's credibility was already supported by the actual fact (bunkhouse exists, is mature, runs in AWS); the duration was a flourish that crossed into fabrication.

## Observation

The orchestrator reaches for quantitative-sounding evidence when constructing arguments — "three years," "9+ code paths," "five categories" — because numbers feel rigorous. When the number is sourced (the "9+ code paths" claim about bunkhouse's mutable-status problem traces to actual research notes; "five categories" is grounded in migration 020), the rigor is real. When the number is invented to fit the argument's shape, it is fabrication regardless of whether the surrounding claim is true.

This is distinct from the collapse-then-restore pattern (`rule:purpose-before-collapse`) and from the commodity-vs-architectural-decision-weight pattern. The failure here is *embellishing-with-invented-precision*: making an argument feel more grounded than it is by inserting numbers the orchestrator has not verified.

## Generalization

We tend to dress up qualitative claims with quantitative flourishes that read as evidence. In an ADR — where every assertion is meant to be checkable later — this is corrosive. Every numeric claim in a Treadmill artifact must be traceable to a source: a research finding, a code reference, a measurement. If we cannot trace a number, we say "long enough to have proven the shape" rather than inventing one.

A related observation: the auto-capture hook from ADR-0008 did not fire on the user's "you've hallucinated" — none of the trigger phrases match. This is a gap in the trigger list. The hook's design accepts trigger-list incompleteness, but "hallucinated," "fabricated," "made that up," and similar phrases are precisely the corrections we want surfaced. Adding them to the trigger list closes the gap without changing the hook's logic.

## Proposed rule

Single observation; below rule threshold per `/rule`. The shape, if a second instance arrives:

> Every numeric claim, named duration, named version count, or quantified-evidence assertion in a Treadmill artifact (ADR, plan, learning, rule) must be traceable to a cited source — research notes, a file/line reference, an actual measurement. If untraceable, the claim must either be removed or rewritten as a qualitative statement.

## Proposed remediation

When the rule lands: an LLM-judge check that scans the artifact for numeric assertions and flags any that lack a citation or traceable source. Severity: warning, since some numbers (counts of items in a clearly-enumerated list) are self-cited.

## Notes

The user caught this manually; the auto-capture hook did not fire. The trigger-list gap is a small operational fix — adding `"hallucinated", "fabricated", "made that up", "made up that"` to `tools/dev-hooks/learning_triggers.json`. I will make that change in the same session that captures this learning, since the gap is the kind of mechanical issue we can close immediately.
