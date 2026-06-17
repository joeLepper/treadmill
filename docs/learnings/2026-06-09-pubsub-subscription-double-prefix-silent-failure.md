---
date: 2026-06-09
trigger: surprise
status: captured
related: plan-2026-06-08-adr-0084-coordinator-implementation
---

# Learning: Pub/Sub streaming pull silently fails on double-prefixed subscription name

## Trigger

Three ramjac chain services (attachment_anonymizer, diagnosis_entity_detector, interaction_detector) deployed to Cloud Run, logged `consumer_ready`, served health checks, but never processed a single Pub/Sub message. Four other services (OCR, classifier, MAR, NTA) worked fine on the same substrate. Debugging spanned gen1→gen2, cpu-throttling, Presidio thread safety, and gRPC fork support before the actual cause was identified.

Root cause (PR #1225): the Pulumi substrate was passing a fully-qualified subscription path (`projects/X/subscriptions/dev-attachment-to-anonymizer`) to `ramjac_events.Consumer`. The `Consumer.run()` method prepended `projects/{project}/subscriptions/` again, producing an invalid double-prefixed name. The streaming pull `SubscriberClient.subscribe()` returned a future that terminated immediately with `InvalidArgument 400` — but since the future was not held or monitored, the termination was silent. The consumer appeared to start correctly (health endpoint live, no exception logged).

## Observation

The four working services passed bare subscription IDs; the three broken services passed full paths. The `Consumer` constructor accepted both without validation. The error was emitted only to the `StreamingPullFuture`, which was discarded at construction time.

## Generalization

Discarded futures from streaming pull hide termination errors completely. Any Cloud Run Pub/Sub consumer that calls `subscriber.subscribe()` and does not call `.result()` or attach a `done_callback` will silently swallow subscription-name errors, subscription-not-found errors, permission errors, and gRPC stream terminations. The service appears healthy (HTTP 200) while never processing a message.

## Proposed rule

Every `StreamingPullFuture` must be: (a) held in a variable, and (b) either `.result()`-blocked or given an `add_done_callback` that logs the termination cause and reason. A streaming pull future that is assigned to `_` or not assigned is a defect.

## Proposed remediation

The `ramjac_events.Consumer.run()` method now (v0.1.6, PR #23) holds the future and attaches a done callback that logs `streaming_pull_future_terminated` with the exception. This fires for every Cloud Run pubsub consumer regardless of how the subscription name is passed. Any future silent termination will surface via this log entry. The complementary fix: validate subscription name format in `Consumer.__init__` and raise immediately on a double-prefixed value.

## Notes

Debugging path: gen1→gen2, cpu-throttling=false, GRPC_ENABLE_FORK_SUPPORT=0, init-order reversal, future.result() instrumentation — none of these changed behavior because the issue was a 400 before any of those factors had a chance to matter. The convergence on `add_done_callback` (from three independent analyses: Bert's gen2 hypothesis + Alan's init-order hypothesis + Donna's gRPC trace) is what finally surfaced the error. The held-future pattern is now the durable fix regardless of what other bugs surface.
