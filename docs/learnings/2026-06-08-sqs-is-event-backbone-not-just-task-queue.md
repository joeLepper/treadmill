---
date: 2026-06-08
trigger: correction
status: captured
related: ADR-0084 (team coordination model)
---

# Learning: SQS is the event backbone, not just a task dispatch queue

## Trigger
While designing the coordinator-based execution model, I twice described SQS as a task dispatch queue and suggested it could be replaced by coordinator-to-worker relay for task assignment. Joe corrected: SQS is the event backbone that delivers external system updates — CI passed, PR merged, PR dirty — into the coordinator so it can turn them into signals for the software team.

## Observation
SQS in Treadmill carries two distinct classes of messages:
1. Initial work dispatch (plan → task → SQS → worker)
2. External system events (GitHub webhooks → API → SQS → coordinator/workers): `pr_opened`, `check_run.completed`, `pr_merged`, `push`, conflict detected, etc.

The second class is irreplaceable. The coordinator needs to know when CI passes on a worker's PR, when a merge unblocks the next task in a plan, when a branch goes dirty. These events originate outside Treadmill's control and SQS is the conduit that delivers them in.

## Generalization
In an event-based system, the queue is not primarily a work dispatcher — it is the integration point between the system and the outside world. Internal work assignment can be done via other mechanisms (relay, direct call), but external event delivery requires a durable, ordered queue that external systems can write to.

## Proposed rule
When evaluating whether to replace SQS: ask separately about internal dispatch (replaceable) vs external event delivery (not replaceable without an equivalent durable subscriber). They are different jobs that happen to share the same transport today.

## Notes
The coordinator in the new model should be the primary SQS consumer for plan-scoped events. It receives CI, review, merge, and conflict signals and routes them to the appropriate worker or handles them directly (e.g. unblocking the next task on merge). The queue stays; what changes is who reads it and what they do with the signal.
