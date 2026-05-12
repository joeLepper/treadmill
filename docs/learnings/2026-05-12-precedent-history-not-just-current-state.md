---
date: 2026-05-12
trigger: correction
status: captured
related: ADR-0007, ADR-0011, ADR-0017
---

# Learning: "Stale" doesn't mean "wrong" — different topology may resurrect the older pattern

## Trigger

Researching the AWS-buffered-webhook path for Treadmill's dev-local deployment, the orchestrator commissioned two research passes against bunkhouse. The first pass read `bunkhouse/infrastructure/lib/bunkhouse-stack.js` (a Jan 30 file) and cited its API Gateway HTTP API → SQS pattern as precedent. The second pass corrected this — the `.js` is a stale compiled artifact; the current source of truth is `bunkhouse-stack.ts` (Mar 18) which uses an ALB → ECS Fargate API → synchronous HMAC pattern with no API Gateway. The second pass concluded "there is no bunkhouse precedent for the AWS-buffered-webhook path Treadmill needs" and labeled the older pattern an "abandoned prototype."

The orchestrator drafted ADR-0017 against the second pass's framing: *"Treadmill is inventing this pattern. The Lambda wrapper for header preservation is a Treadmill-specific addition."*

The user pushed back: *"I think that you're wrong but only because the version of bunkhouse that did this was replaced by the cloud-native version. It's probably worth looking back through git history in bunkhouse to see if you can look this up."*

Git archaeology confirmed: commit `d357e47e` on 2026-01-29 ("Add AWS SQS infrastructure via CDK") shipped API Gateway HTTP API → SQS direct integration via `CfnIntegration` with `integrationSubtype: 'SQS-SendMessage'` and `requestParameters: { QueueUrl: ..., MessageBody: '$request.body' }`. Three routes: `/webhook`, `/webhook/github`, `/webhook/slack`. The pattern was real, shipped, and operational. It was replaced when bunkhouse moved its API service into AWS (ALB+ECS), making the buffered-webhook path obsolete *for bunkhouse's topology* — not because the pattern was flawed.

## Observation

The orchestrator's first reflex when encountering "an old artifact" was "this is stale, ignore it." That reflex is right when the artifact was abandoned because the *pattern* was wrong. It is wrong when the artifact was retired because the *topology changed*.

Two distinct reasons an artifact becomes "stale":

- **Pattern superseded.** The team learned the approach didn't work and replaced it with something different. The old artifact is a *learning* about what didn't work — citing it as precedent is reversed-causation reasoning.
- **Topology changed.** The team's deployment context moved (in this case: bunkhouse moved its API into AWS, making buffered-webhook unnecessary). The artifact still records *how the pattern worked when the context required it*. For a downstream system whose topology matches the old context, the old artifact is the *right* precedent.

Bunkhouse moved its API into AWS for reasons unrelated to webhook-buffering: it wanted a single deployment unit, public ALB, RDS-managed state, the operational benefits of a cloud-native service. That topology change made the webhook buffer obsolete because the API itself was reachable from GitHub directly. Treadmill's dev-local explicitly *can't* run the API in AWS (the constraint is "personal-Treadmill on minimal AWS, no Joe-money-for-employer-resources-or-vice-versa"). Treadmill's topology matches bunkhouse's Jan 29 topology, not bunkhouse's Mar 18 topology.

The right precedent question wasn't "what does bunkhouse do today?" but "what did bunkhouse do when bunkhouse's API was outside AWS, and why did that change?" The git log answers both.

## Generalization

Before citing a precedent project's *current* code as the authority, ask the historical question:

> Did this codebase ever ship a *different* approach for this problem? If so, why did they move away from it? Is the move a learning-about-the-pattern, or a topology-change that doesn't apply to me?

Concrete operations:

- **Read git log for the file**, not just the file. `git log --all --oneline -- <file>` is the cheapest historical inspection.
- **Read commits with subject lines matching the problem.** `git log --grep webhook` / `git log --grep sqs` surfaces architectural decisions even when the relevant file moved.
- **Read commit *bodies*, not just subjects.** The Jan 29 commit message said "Direct SQS integration (no Lambda needed)" — that was a design statement worth surfacing in the ADR.
- **Check whether the abandonment commit explicitly rejects the pattern**, or just moves to a different topology. Bunkhouse's later commits added ALB + ECS + RDS — none of them said "the APIGW+SQS pattern is wrong"; they said "we're moving the API into AWS."

The trap is reading code archaeologically as a *snapshot*. A snapshot says "today's code does X." A history says "X was tried, then Y was tried, and they chose Y because Z." Z is the load-bearing fact.

## Generalization beyond bunkhouse

This is the same shape as the `bunkhouse-precedent-on-shape-decisions` learning, but one level deeper:

- The earlier learning said "when bunkhouse has solved a shape question, default to its answer."
- This learning says "but 'its answer' is the answer at the time bunkhouse's context matched yours, not necessarily the answer in today's bunkhouse."

The two compose: when a Treadmill design decision matches bunkhouse's current topology, crib current bunkhouse. When Treadmill's topology matches an *earlier* bunkhouse, crib the earlier version. The git log is the disambiguator.

## Proposed rule

A candidate, not yet a rule. The shape would be:

> *Before citing a precedent project's code as authority, run `git log` against the relevant file and commit-subject grep. Surface the historical context in the ADR — both today's pattern and any prior shapes the project shipped. If today's pattern reflects a topology change rather than a pattern rejection, the prior shape may be the right precedent for a different topology.*

Wait for one more instance before crystallizing. This is currently three instances of "go look at bunkhouse for precedent" — once for output-shape (uniform-envelope, this learning's predecessor), once for plan-durability (durable-files), and now this one. The shape question is well-established; the *git-history* question is the new wrinkle.

## Proposed remediation

None yet — wait for the rule. But the practical application is immediate: update ADR-0017 to reflect the corrected history (the APIGW→SQS pattern is bunkhouse-precedent-from-Jan-29, not a Treadmill invention; the Lambda wrapper is a Treadmill addition that preserves headers, which the original bunkhouse pattern doesn't require because bunkhouse's original consumer didn't appear to do signature verification — or did so via a different mechanism we should check).

## Notes

The auto-capture hook caught "you're wrong" — the explicit-correction phrase, which is the highest-signal trigger in ADR-0008's trigger list. The user's correction was specific and immediately verifiable via git archaeology; the orchestrator should have run that archaeology before drafting the ADR in the first place.

This learning pairs with `2026-05-08-fabricated-supporting-evidence.md` — both are about resisting confident-sounding research that turns out to skip the verification step. The first researcher said "the current truth is `.ts`, the `.js` is stale" — true on a snapshot, misleading on history. Future research agent briefs should request *historical* context, not just current-state.
