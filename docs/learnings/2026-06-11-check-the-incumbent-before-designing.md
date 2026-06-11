---
date: 2026-06-11
trigger: correction
status: captured
related: ADR-0088 (superseded same night), docs/plans/2026-06-10-prod-promotion-gate-contract.md
---

# Learning: check the incumbent system before designing a replacement

## Trigger
Joe, on discovering the ADR-0088 prod-promotion gate (contract + API +
CLI + templates, built and merged overnight): "This is wrong. You should
have looked at how this used to work. We used GitHub environments and
approved reviewers before and that's the system that we should use going
forward. Treadmill is my system. It is separate from medicoder."

## Observation
Four sessions designed, contract-reviewed, implemented, and cross-reviewed
a human-approval gate for prod deploys — contract-first, sibling-validated,
well-tested — without anyone asking how medicoder's previous deploy
pipeline handled human approval. The answer (GitHub environment protection
with required reviewers) was the platform-native feature the project had
already used. The review process caught design flaws INSIDE the chosen
frame (the expiry CAS hole, the evidence floor) but never challenged the
frame itself. A second boundary error compounded it: the gate put
repo-deploy control into Treadmill, whose owner scopes it to team
orchestration, not deploy mechanics.

## Generalization
Sibling review converges within the proposer's frame. The cheapest,
highest-leverage review question — "what does the existing system do, and
why isn't that sufficient?" — is also the one a same-frame reviewer never
asks. We already had this rule for bunkhouse precedent
(adopt-by-default, deviate with documented reason); the lesson is that it
applies to EVERY incumbent, including the platform's native features and
the project's own history, not just our prior art.

## Proposed rule
Every ADR carries an Alternatives section whose FIRST entry is the
incumbent/native solution ("what do we do today / what does the platform
already provide"), with a stated reason it is insufficient. An ADR
without it is incomplete; reviewers reject on that basis.

## Proposed remediation
Add the requirement to the /decide skill template. Reviewer checklist
line: "Does the Alternatives section name the incumbent, and is the
rejection reason convincing?"

## Notes
The frame-checking failure cost one night of four-session work (the
revert is mechanical; the cross-review machinery all worked as designed —
on the wrong question). System-boundary corollary: a control-plane owner
defines what the control plane is FOR; check that boundary explicitly
when a design makes one system govern another's mechanics.
