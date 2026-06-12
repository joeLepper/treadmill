# tools/team-templates/coordinator/

ADR-0087 coordinator-session template tree. Parallel structure to
`tools/team-templates/worker/` + `tools/team-templates/evaluator/`
(both from ADR-0087 PR-E by Carla).

## What lives here

- `CLAUDE.md.tmpl` — the canonical coordinator system prompt. Encodes
  the full ADR-0087 lifecycle: §3 event handlers (`plan.submitted`,
  `task.registered`, `pr_merged`, `task.ci_result` — the ADR-0090
  per-suite rollup, replacing `check_run.completed` —
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

## Recent changes

- **§3.5 per-check handler → `task.ci_result` rollup handler (task 257b19a2, ADR-0090)**: the old `github.check_run.completed` handler — per-check advance ("last required check"), per-check rework, and the coordinator hand-writing `task.ci_result` via the manual-events surface — is DELETED (the deletion is a plan success criterion; pinned by test). The new §3.5 consumes the API-observer's per-suite rollup (#336 payload contract: `repo/pr_number/head_sha/check_suite_id/conclusion/app_slug`) and fires the same decisions ONCE per suite: peer review on suite-success, coordinator-rework on suite-failure. Four #336-review carry-forwards are contractual in the handler text and pinned by tests: terminal-task tolerance (closed-PR heads DO emit; ignoring their ci_result is the system working), per-suite cardinality + the `app_slug == 'github-actions'` consumer-policy filter, the serialized-ingest dedup caveat (same-conclusion redelivery collapses upstream ONLY while ingest stays sequential — the handler must stay idempotent), and repo case-matching intent (observer fallback case-insensitive vs #335 resolver exact-case; register task_prs with canonical casing). Wake config deliberately untouched (next task): a TRANSITION NOTE marks check_run wakes as no-action until the wake-filter lands. §7.1 rework-brief composition updated from per-check details to the suite rollup (`gh pr checks` names the failing checks).
