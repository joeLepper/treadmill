---
date: 2026-06-09
trigger: correction
status: captured
related: ADR-0084, ADR-0086
---

# Learning: Orchestrator/Coordinator/Worker role model was inverted in ADR text

## Trigger
During ADR-0085+0086 implementation, Alan mis-stated that `created_by` on a submitted plan should be set to `coordinator-medicoder`. Joe corrected: the coordinator is a PM, not the executive-in-charge; the orchestrator who submits the plan is the executive. The correction exposed that CLAUDE.md and ADR-0086 had encoded the wrong role hierarchy — what the ADRs called "orchestrators" (Bert/Donna/Carla) were actually the coordinators' domain in Joe's model.

## Observation
The three-tier model Joe designed is:
1. **Orchestrators** (Alan, Bert, Carla, Donna): long-lived named sessions Joe talks to directly. Research, ADRs, plan authoring, plan submission. Executives-in-charge. Stopgap escalation targets for coordinators.
2. **Coordinators** (coordinator-medicoder, etc.): PMs per repo. Receive plans from orchestrators, direct workers, own lifecycle signal (gate failures, merge conflicts, PR registration). Escalate to an orchestrator when stuck.
3. **Workers**: frontline implementers. Write code, open PRs, communicate laterally with peers and upward to their coordinator.

CLAUDE.md (and verbally during implementation) used "orchestrator" to mean "session that writes code" — i.e., what Joe calls a worker. This caused `created_by` to be auto-set to `coordinator-medicoder` on plan submit, which is semantically wrong: `created_by` should be the orchestrator session (e.g., `treadmill-alan`) that actually submitted the plan.

## Generalization
Role labels in Treadmill have precise meanings Joe has defined. When an ADR encodes a different meaning for the same label, the ADR is wrong — not Joe's model. Before locking in a role label in code or configuration, verify against Joe's mental model, not against prior ADR text.

## Proposed rule
When an ADR uses a session-role label (orchestrator, coordinator, worker), cross-check the label definition against the living role hierarchy Joe maintains before encoding it in code or config. If the ADR and Joe's model diverge, treat Joe's model as authoritative and flag the ADR for correction.

## Proposed remediation
- ADR correction task whenever a role-label divergence is found
- CLAUDE.md is the canonical session-role reference; keep it in sync with Joe's model
- `created_by` on plan submit must never be auto-set to a coordinator label; it should preserve the submitting orchestrator's label

## Notes
Downstream code impact from this mis-encoding:
- `services/api/treadmill_api/routers/plans.py` lines 526-528: auto-sets `created_by = team_config.coordinator_label` — needs revert
- Coordinator WS subscription filter on `created_by=coordinator-medicoder` will no longer match; needs a repo-based or separate routing mechanism
- CLAUDE.md roles section needs rewrite
- ADR-0086 role definitions need correction
