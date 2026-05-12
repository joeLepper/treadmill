---
date: 2026-05-07
trigger: correction
status: crystallized-into-rule-features-ship-with-tests
related: plan-2026-05-07-local-adapter-spike
---

# Learning: Features ship with tests

## Trigger

During Day 1 of the local adapter spike, we shipped a working `treadmill-local up` / `down` / `status` lifecycle — moto provisioning, CDK synth parsing, container management — without any automated tests. The user pushed back: *"can you solidify what you've already done with tests? I think that one of our first learnings / rules / remediations will be about ensuring that features ship with tests."*

## Observation

Forward momentum on a spike biases toward "make it work" over "prove it works." When success criteria are observable manually (a CLI prints success messages, a round-trip echoes a payload), the absence of automated tests is invisible at the time of shipping. The author moves on; the next person inherits a working artifact with no regression net.

## Generalization

We tend to prioritize visible progress over verifiable progress, especially during dogfooding spikes where the substrate is changing fast. Tests are easier to skip when the work is "obviously" correct, but that's exactly when latent assumptions are densest — early code carries the most invariants per line. A spike without tests becomes load-bearing without anyone deciding it should be.

## Proposed rule

A feature is not "shipped" until it has tests that exercise its primary success criteria. Spike code that intentionally elides tests must declare so explicitly in its plan, with an attached follow-up to add them before the spike's outcome is locked.

## Proposed remediation

Two layers, hybrid per ADR-0002's spirit:

1. **Deterministic.** A pre-merge check (initially a hook, eventually part of the validation pipeline) fails when a PR adds production code under `infra/`, `tools/`, `services/`, or `workers/` without adding or updating files under a `tests/` sibling.
2. **Non-deterministic.** An LLM judge inspects PRs whose code surface has tests but whose tests don't appear to exercise the primary success criteria — catching the "wrote a test, but it doesn't test the right thing" case the deterministic check cannot see.

Both should be authorable as rules with attached remediations once the `/rule` skill and rule engine exist.

## Notes

This learning was itself produced by the very pattern it describes: we had to be told, in conversation, that tests were missing. If the orchestrator's auto-capture path described in `.claude/skills/learning/SKILL.md` had existed, it would have triggered on the user's correction and drafted this learning automatically. That path is the right destination; manual capture is the bootstrap.
