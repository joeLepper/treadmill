# tools/team-templates/coordinator/

ADR-0087 coordinator-session template tree. Parallel structure to
`tools/team-templates/worker/` + `tools/team-templates/evaluator/`
(both from ADR-0087 PR-E by Carla).

## What lives here

- `CLAUDE.md.tmpl` — the canonical coordinator system prompt. Encodes
  the full ADR-0087 lifecycle: §3 event handlers (`plan.submitted`,
  `task.registered`, `pr_merged`, `check_run.completed`,
  `pull_request.synchronize`), §5 dispatch ordering (POST
  task_execution → pre-drain inbox → cc-relay brief), §7 CI + conflict
  loop, §8 peer review (1–2 reviewers, parallel, collation, re-cycle
  semantics), §9 evaluator handoff (brief format, verdict parsing,
  approve/rework split, max-cycles cap, timeout escalation), and §2
  startup recovery (stale-row sweep, inbox drain, mergeability
  re-poll, events-table replay). `{{REPO_SLUG}}` is the only
  placeholder; the installer substitutes the per-team value.

## Installation

The CLI wiring follow-up (after ADR-0087 PR-D + PR-E merge) extends
`tools/team-templates/install.py` to install the coordinator's
rendered CLAUDE.md alongside the worker + evaluator ones, at
`~/.treadmill/teams/<slug>/coordinator-<slug>/CLAUDE.md`. The
operator-step to land this template on a live coordinator is the
restart sequence between PR-D merge and PR-F (Phase 4 table drops) —
the live session picks up the new template on respawn.

## Relationship to `tools/coordinator/coordinator_prompt.md`

The legacy v1 prompt (ADR-0084 / ADR-0086 lifecycle) is deprecated
at `tools/coordinator/coordinator_prompt.md`. ADR-0087 Phase 5
removes that file together with the legacy table drops. Until
Phase 5 lands, the v1 prompt stays as a reference for any
pre-ADR-0087 coordinator session that has not yet been restarted.

## Tests

`tools/team-templates/tests/test_coordinator_template.py` covers
structural assertions: placeholder substitution, presence of the
required handler sections, single-writer invariant language, trust
boundary language, evaluator timeout numbers (30 / 60 min), the
four-value trigger taxonomy. The point is to fail loudly if a
future edit accidentally drops a load-bearing section; we do not
LLM-judge the full prompt content (the operator owns prose
quality, the test owns shape).
