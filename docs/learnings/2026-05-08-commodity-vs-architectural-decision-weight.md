---
date: 2026-05-08
trigger: correction
status: captured
related: ADR-0011
last_crystallization_check: 2026-05-17
crystallization_backoff_until: 2026-05-24
crystallization_target: pending-second-instance
---

# Learning: Calibrate pedantry to decision weight; do not defer commodity choices

## Trigger

While planning Phase 2, we proposed deferring a tech-stack ADR because "if we hit a real fork we author then." The user pushed back: *"I don't think that we need to defer. Most of the tech decisions have already been made when we developed Bunkhouse. What we're doing right now is wrapping that technology in better opinions and a discrete planning flow. No need to be pedantic, just a quick list of what we'll use and why. A place you might want to be pedantic: why we use event-driven, immutable architecture."*

We were treating commodity choices (FastAPI, Postgres, alembic, Redis) and architectural opinions (event-driven coordination, immutable state) with the same weight — and applying the same default of *defer*. That weight calibration is wrong.

## Observation

When the orchestrator anticipates many open decisions, the default behavior is *defer them all*. The implicit rationale is "we'll author when we hit a real fork." But not all open decisions are forks — some are settled by precedent (bunkhouse already proved them) and need only a brief stating-for-the-record. Treating them as forks creates documentation debt without reducing risk.

Two distinct decision modes:

- **Commodity decisions.** A bullet-list with brief rationale is sufficient. The decision is settled; the ADR records *what* and is terse about *why*.
- **Architectural decisions.** The reasoning is the value. The ADR records *why* in depth, lists alternatives, names the trade-offs explicitly. Defer-by-default is fine here when the decision is genuinely open; when it is settled, the ADR is heavyweight.

## Generalization

The orchestrator should learn to ask, before deferring: *is this decision settled by precedent (bunkhouse, ADR-0001's opinions, prior plans), or is it a genuine open fork?* Settled decisions get authored quickly with bullets. Open forks get deferred or explored properly. Mixing the modes — bullets for an architectural opinion, or heavyweight ADRs for commodity — is the failure to look out for.

This is a different shape from `rule:purpose-before-collapse` (which is about removing structure). Here the failure is treating decisions of unequal weight as equal. Both are calibration failures, but the calibration axis differs.

## Proposed rule

A candidate, but a single instance is below threshold per the `/rule` skill (single observations rarely earn enforcement). Watch for a second instance before crystallizing. If a second instance arrives, the rule shape is something like: *before deferring a decision, classify it as commodity (precedent-settled) or architectural (genuine fork); commodity gets authored terse, architectural gets full treatment or an explicit deferral with a fork criterion.*

## Proposed remediation

None yet — wait for the rule.

## Notes

The auto-capture hook caught the substring "i don't think" again and the substring was the actual correction this time (not a false positive like the 2026-05-08-per-role-images-collapse-attempt instance). The hook continues to be useful even when the trigger fires on a phrase the orchestrator might otherwise read as casual.
