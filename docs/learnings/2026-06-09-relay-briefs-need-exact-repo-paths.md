---
date: 2026-06-09
trigger: correction
status: captured
related: plan-2026-06-08-adr-0084-coordinator-implementation
---

# Learning: Relay task briefs need exact repo paths, not inferred repo names

## Trigger

When briefing Carla on the medicoder IAMBinding sweep task, Alan wrote "medicoder-gcp-substrate repo" — an inference from Donna's description of the path `infrastructure-gcp/`. The correct location is `infrastructure-gcp/` directory inside the main medicoder repo. Carla caught the discrepancy immediately (she had reviewed multiple medicoder substrate PRs that day) and held before starting until the path was confirmed.

## Observation

The operator relayed a directory path (`infrastructure-gcp/`); the coordinator inferred a repo name from it and relayed that name to a sibling. The sibling had ground-truth knowledge (from prior PRs) that contradicted the inferred name and correctly blocked action until confirmed.

## Generalization

When we author task briefs for sibling sessions, we tend to paraphrase source information rather than quote it directly. Directory paths inside repos frequently look like repo names or package names, and an incorrect repo name in a task brief will either stall the sibling (as here) or cause them to operate on the wrong codebase. Sibling sessions have better ground-truth than the coordinator for repos they've recently worked in.

## Proposed rule

Task briefs that reference a specific file location must state both the repo and the path within it explicitly (e.g., "infrastructure-gcp/ inside MediCoderHQ/medicoder"), never just a directory name.

## Proposed remediation

When a sibling reports a "doesn't match" or path discrepancy in response to a relay brief, treat it as a required correction before re-sending — the sibling's prior-PR context is the authoritative source. The correction should go out as a `--type action` re-relay (not a context message) if the original brief was an action request.

## Notes

Also surfaced in this incident: the original relay was sent as `--type context` (missing `--type action` flag), so Carla correctly held on the action-request trust gate. Both issues resolved in the corrected re-relay.
