# tools/dev-hooks

## Purpose

This directory contains Claude Code hooks that run during Treadmill author sessions to capture learning candidates, surface pitfalls, and assist with durable documentation capture. These hooks implement the learning capture discipline described in ADR-0008: they scan user prompts for correction phrases, suggest candidates for formalization, review pending changes for undocumented pitfalls, and surface actionable feedback without blocking the author's flow. The hooks are advisory; they surface candidates for the orchestrator's judgment rather than enforcing policy.

## Key surfaces

- `capture_learning_candidate.py` — UserPromptSubmit hook; scans user prompts for correction-phrase patterns and appends candidates to `.treadmill-local/learning-candidates.jsonl`.
- `review_candidates_at_stop.py` — UserSessionStop hook; reviews pending changes against the knowledge base and surfaces undocumented pitfalls or broken rules as additionalContext.
- `learning_triggers.json` — configuration file defining which phrases to watch for, rate limits, and false-positive exclusions.

## Recent changes

> **New entries are PER-PR FRAGMENT FILES, not prepends** (task
> 986c5cf6): add `agent-changes/YYYY-MM-DD-<task-or-pr-slug>.md` beside
> this AGENT.md — one entry per file, newest by filename; format in
> `docs/agent-md-schema.md`. Prepending here is the conflict factory
> that stacked three same-day rework cascades on 2026-06-12 (every
> in-flight PR inserts at this same anchor). Entries below predate the
> convention and are frozen; gardening folds them into the sections
> above.

- ADR-0030 plan + backfill — hooks are part of the federated in-repo agent context initiative.
- ADR-0008 — learning capture skill + hook triggers; established the learning taxonomy and hook contract.
- [#33](https://github.com/anthropics/treadmill/pull/33) — First Treadmill-specific rules in `docs/knowledge-base/rules/`.

## Pitfalls

- Hook execution is synchronous and must complete within a timeout; long-running scans (e.g., expensive regex or knowledge base lookups) can cause the session to stall. Keep hook logic fast; defer expensive validation to async tasks if needed.
- The learning candidate queue (`.treadmill-local/learning-candidates.jsonl`) is append-only and not automatically pruned; old candidates accumulate and can clutter reviews. Periodically audit and remove resolved candidates.
- False positives in correction-phrase triggers cause context bloat; the `learning_triggers.json` false_positive_skips list is the escape hatch, but it grows unboundedly if not audited. Keep the list small and explicit.
- Learning capture is advice-only; authors can ignore hook candidates and push changes without formalized learnings. This is sometimes correct (ephemeral one-offs) but can leave gaps in documentation. Monitor merged PRs for patterns that should have surfaced learnings but didn't.

## Navigation

- **Adjacent:** `docs/` (learnings and rules live here); `services/api/`, `workers/agent/` (run author sessions that these hooks monitor).
- **Decisions:** ADR-0008 (learning capture skill + hook triggers); ADR-0030 (federated in-repo agent context).
- **Follow:** Read ADR-0008 for the learning taxonomy and hook contract; review `learning_triggers.json` for the phrase patterns and rate limits.
