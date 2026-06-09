---
date: 2026-06-09
trigger: pattern
status: captured
related: plan-2026-06-08-adr-0084-coordinator-implementation
---

# Learning: Default-empty escape hatch is a silent-failure class

## Trigger

Three distinct incidents in the same Phase 5 session surfaced the same structural failure mode:
1. `gh` API returning an empty list on an expired token (gh CLI swallows auth errors on list endpoints)
2. `envsubst` substituting empty string for an unset variable (`EC2_PUBLIC_IP=` → otel-collector config had bad value; learning `2026-06-09-envsubst-empty-substitution-silent-secret.md`)
3. `StreamingPullFuture.subscribe()` returning a future that terminated immediately on a bad subscription name — silently, because the future was discarded (learning `2026-06-09-pubsub-subscription-double-prefix-silent-failure.md`)

Bert's post-mortem named the common shape: "any API contract where 'return value or error' has a default-empty escape hatch is a silent-failure surface."

## Observation

Each API accepted a bad input (expired token, unset var, malformed subscription name) and returned something structurally valid but semantically empty (empty list, empty string, live-looking but immediately-terminating future). The caller received no error, continued execution, and the failure surface was the downstream behavior — empty dashboard list, secret with bad content, consumer that never processed a message.

## Generalization

Wherever a data pipeline step has a "succeed with empty" path alongside a normal-success path, the empty outcome is indistinguishable from the error outcome without explicit post-validation. Silent-empty is more dangerous than a thrown exception because it delays discovery to the first consumer that notices the emptiness.

## Proposed rule

At every consumption site of an API that can silently return empty: assert the result is non-empty before proceeding. For futures: hold the reference and attach a termination callback. For template substitution: fail-fast on unset variables (`set -u` or `${VAR:?error}`). For CLI list commands: check exit code AND result length.

## Proposed remediation

A pre-commit or CI check that flags: `envsubst` usage without `set -u` or `${VAR:?}` guards; `StreamingPullFuture` assignments that are not held or do not have `add_done_callback`; any `gh ... list` piped into a downstream command without a length check.

## Notes

Related: [[2026-06-09-envsubst-empty-substitution-silent-secret]], [[2026-06-09-pubsub-subscription-double-prefix-silent-failure]]. Bert's Phase 5 post-mortem proposed crystallizing these three into a single ADR or rule next session.
