# docs

## Purpose

This directory is the durable knowledge base for Treadmill, holding the architectural decisions, plans, learnings, rules, and runbooks that govern how the system works and how authors interact with it. It is the single source of truth for "why we built it this way" — divorcing context from chat. The three-layer documentation model (ADR-0003) divides this space: ADRs capture design decisions and their tradeoffs, plans record task hierarchies and success criteria, learnings preserve surprising patterns or incidents, and rules formalize learnings into enforceable policy with remediations.

## Key surfaces

- `adrs/` — Architectural Decision Records; each ADR captures a non-trivial design choice with context, decision, alternatives, consequences, and a mermaid diagram where appropriate. Indexed by sequential number (0001, 0002, ..., 0030).
- `plans/` — Project plans; each plan records a complex task's goal, success criteria, scope (including out-of-scope), sequence, risks, and any diagrams. Plans are mutable in-flight and gain a postmortem when complete.
- `learnings/` — Discrete learnings; each captures a surprising discovery (a pattern, a mistake, an incident) that informs future decisions. Learnings feed the rule-formalization pipeline.
- `knowledge-base/rules/` — Enforceable policy; rules formalize validated learnings into deterministic checks (grep, schema validation) or advisory recommendations (llm-judge). Each rule has a remediations section and a severity level.
- `handoffs/` — Handoff documents; role responsibilities, skill descriptions, and integration points for Claude Code roles.
- `runbooks/` — Operational guides; procedures for on-call, deployments, incident response, and other recurring tasks.

## Recent changes

- [#39](https://github.com/anthropics/treadmill/pull/39) — /decide and /plan skills carry ADR-0004 checklist + diagram-type guidance table.
- [#38](https://github.com/anthropics/treadmill/pull/38) — AGENT.md schema document (docs/agent-md-schema.md) and validation rules.
- ADR-0030 + plan — federated in-repo agent context (mermaid diagrams + AGENT.md files at component roots).

## Pitfalls

- ADRs reference each other but have no validator to catch stale cross-references; broken links in "Related" or "Follow" sections can silently guide authors to the wrong decision. When moving or renaming ADRs, audit all backlinks.
- Plan documents live in `docs/plans/` but are referenced from the workflow database (ADR-0028); stale plan files on disk (e.g., orphaned by a manual delete) can cause confusion. Always update the database record before deleting a plan.
- Learning documents accumulate over time and are not automatically deduplicated; similar learnings from different incidents can sit side-by-side without cross-reference. Periodically audit and consolidate related learnings.
- Rules are enforced asynchronously by the validation runner (ADR-0029); if a rule definition changes, previously-passing workflows are not re-evaluated. Changes to rule logic are high-risk; always snapshot the old behavior before amending.
- **Confirm deployment mode before incident diagnosis.** Treadmill has two modes (dev-local and ECS production) with entirely different observability surfaces. Before diagnosing stuck tasks, missing PRs, or silent failures: run `docker ps | grep treadmill` to confirm dev-local workers are up, or check `TREADMILL_DEPLOYMENT_MODE` in the local-adapter config. Consulting the wrong surface produces misleading signals. (Learning: `docs/learnings/2026-05-15-identify-deployment-mode-before-diagnosing.md`)

## Navigation

- **Adjacent:** All components (`services/api/`, `workers/agent/`, `infra/`, `tools/local-adapter/`, `tools/dev-hooks/`) reference this docs directory for their governance and design context.
- **Decisions:** ADR-0003 (three-layer documentation model); ADR-0004 (diagrams as contract of intent); ADR-0006 (rules and remediations primitive); ADR-0008 (learning capture skill + hook triggers); ADR-0030 (federated in-repo agent context).
- **Follow:** Start with ADR-0001 for the foundational thesis; read ADR-0003 to understand the three-layer model; use ADR-0004's diagram-type table when authoring new decisions.
