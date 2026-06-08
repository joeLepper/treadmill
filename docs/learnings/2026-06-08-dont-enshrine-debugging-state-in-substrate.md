# Don't enshrine debugging state in substrate-as-code

**Date:** 2026-06-08
**Context:** Medicoder Plan C substrate-reconciliation, Stream 5 wrap (pulumi-up follow-up on 2 subscription filter REPLACEs)
**Tags:** `pulumi`, `substrate-as-code`, `reconciliation`, `target-state`

## The trigger

Pulumi preview surfaced 2 `REPLACE` operations on `attachment_to_mar` and `attachment_to_nta` subscriptions. The cause: the substrate (`infrastructure-gcp/src/constants/pubsub.ts`) declared strict CEL filters that required `attributes.classification` and `attributes.clinical_class`. The deployed state had relaxed action-only filters because Donna had loosened them via gcloud while debugging the chain — the upstream classifier wasn't yet setting those attributes, so the strict filters dropped every message.

The naive substrate-reconciliation move would have been to update `constants/pubsub.ts` to match the loosened deployed filters. That would have eliminated the diff and "converged" the substrate.

Donna refused:

> The action-only loosening is a debugging step, NOT target state. We don't want to enshrine it in substrate. Carla's classifier-attributes patch (Stream 3) will set `classification` + `clinical_class` on the outbound CLASSIFIED envelope. Once that lands + classifier rebuilds + redeploys, the strict substrate filters work correctly and the chain flows MAR + NTA cleanly.

The right move was option (b): leave the substrate strict, mark the 2 REPLACE URNs as do-not-converge until the upstream fix lands, then run a targeted `pulumi up --target=attachment_to_mar --target=attachment_to_nta` after Carla's PR merges to re-tighten the deployed filters back to target state.

## The principle

**Substrate-as-code (Pulumi, Terraform, CDK, etc.) declares target state — not transitional debugging state.** When deployed state diverges from substrate because a human ran an out-of-band fix to unblock something:

1. If the divergence is the **new target state** (a permanent improvement, an operational decision being captured): update substrate to match, apply normally.
2. If the divergence is **temporary debugging state** (loosened filters, dropped policies, manual workarounds for an upstream bug): **DO NOT** update substrate to match. Gate the convergence on the upstream fix landing.

Confusing the two locks the workaround into the system. Future operators reading the substrate would assume the loosened filter is what was *meant* — they'd have no reason to tighten it back, and the bug the loosening worked around silently lives forever as a feature.

## How to apply

When reconciling substrate after operators ran gcloud-direct / terraform-direct / kubectl-direct fixes during an incident or debugging push:

- For each divergence, ask the operator: **was this fix the new target state, or transitional?**
- For target-state fixes: update substrate to match, apply, ship.
- For debugging fixes:
  - Leave substrate as-is.
  - Document the gap clearly (PR description, learning doc, inline comment on the substrate file's relevant line).
  - Identify what upstream change closes the gap (e.g. "Carla's classifier-attributes patch on PR #1XXX").
  - When that upstream change lands, run a targeted apply (`pulumi up --target=...`) to re-tighten.
- If you can't determine which category a divergence falls into, default to "ask the operator." Wrong-direction substrate updates are silent failures.

## Cross-references

- [Cloud Run command/args bypass entrypoint](../../../medicoder/docs/learnings/2026-06-07-cloud-run-command-args-bypasses-entrypoint-skips-alembic.md) — same shape, different domain: a workaround captured into substrate locked in an alembic-bypass for a window. The substrate fix (Dockerfile CMD) was the structural close; the substrate-capture of the override was the temporary bridge.
- [feedback-pulumi-iambinding-authoritative-race](../../../.claude/projects/-home-joe-treadmill/memory/feedback_pulumi_iambinding_authoritative_race.md) — Donna's gcloud-direct cloudtrace.agent grant on otel-collector WAS target state (the substrate just had the IAMBinding-loop bug). That made it a category-1 reconciliation (update substrate, apply, done). Category matters.
