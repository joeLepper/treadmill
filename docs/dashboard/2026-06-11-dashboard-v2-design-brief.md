# Dashboard v2 design brief — the post-ADR-0087 operator surface

- **Status:** drafting (sibling review → Joe → Claude Design)
- **Date:** 2026-06-11
- **Related:** ADR-0087 (team execution model), ADR-0089 (token economics),
  ADR-0092 (data migration gates), dashboard v1 plan (2026-05-26)
- **Author:** treadmill-bert; reviewers Alan (model fidelity), Carla
  (staging/telemetry surfaces), Donna (ops/alerting panels)

## Why a v2

Dashboard v1 renders a system that no longer exists. In 48 hours
(2026-06-09 → 11) the execution model was replaced end-to-end: the
autoscaler fleet, workflow_runs/steps pipelines, role machinery, and the
DSPy review queues are deleted; the live system is per-repo teams
(coordinator + evaluator + N workers) driving tasks through
dispatch → CI → peer review → evaluator → merge, with task_executions +
llm_calls as the state spine and the events table as the nervous system.
v1's SQL was ported in PR-F so the pages still load — but they synthesize
one-step pipelines from a model that has real, richer stages to show.

This brief is the data-grounded input for a Claude Design pass, in the
v1 tradition: correct field shapes first, visual exploration second.

## Screens

### S1 — Team roster (replaces: fleet panel)
Per repo: coordinator / evaluator / workers with session liveness
(systemd unit state + last-event-seen), current task per worker
(running task_execution: task title, trigger, started_at), per-worker
counts today (executions by trigger, reviews done). The "is the system
alive" view.
SEMANTICS (Alan fold, from the 2026-06-10 false-stall lesson):
task_executions rows span until PR MERGE, not subprocess exit — a
worker legitimately carries TWO running rows (one awaiting merge, one
active subprocess). "Current" = most-recent-started running row;
awaiting-merge rows render as their own visual state, never as a
double-booked worker.
DATA: team_configs; task_executions (status='running' join tasks);
events recency per label. Liveness needs one new tiny surface: a
session-heartbeat (systemd state exporter or last-WS-activity per
label) — the only net-new data requirement in this brief.

### S2 — Task loop pipeline (replaces: workflow_run_steps strips)
Per task: the real stages — dispatched → CI → peer review → evaluator
→ merged — each stage stamped from its event (task_executions row;
task.ci_result; github.pr_review_submitted + task.peer_review_verdict;
task.evaluator_verdict; github.pr_merged). Rework cycles render as
loop-backs on the same strip (cycle count badge per stage), not as
separate runs. The v1 task-detail "runs" list becomes the execution
history (trigger, worker, duration, tokens per cycle).
DATA: all exists — task_executions + the five event actions.
Two stage-rendering semantics (Alan folds): (1) a held-at-merge state
on the strip, distinct from executing (same await-merge semantics as
S1); (2) the merged stamp badges Path-B backfilled merges (payload
carries backfill:true + backfilled_by + reason) — the audit
distinction exists in the data and the UI must not flatten it.

### S3 — Cost per outcome (HEADLINE — Joe directive 2026-06-11)
The company-facing economics signal, two levels:
- TASK: cost per merged PR, decomposed by execution cycle + trigger —
  "what did the rework cost" is a number next to the outcome.
- PLAN: total cost per delivered plan; cost trend across plans.
DATA: llm_calls → task_executions → tasks → plans; outcome =
derived_status pr_merged/done.
REQUIREMENT (flagged, load-bearing): a per-model pricing table with
cache-reads priced at their discounted rate. The v1 flat
_USD_PER_TOKEN is a placeholder; cache reads are ~85% of API-equivalent
volume at a very different unit price — a flat ratio distorts every
number on this screen. Pricing table is config, not schema.
DEFERRED (explicitly): story points as the denominator (cost-per-point).
Design the task row so a points column attaches later without rework;
ship nothing for it now.

### S4 — Drafts ledger (NEW — Joe directive 2026-06-11)
Every plan + ADR across both repos with its pipeline-of-intent
position: draft → sibling review → PR open → merged → submitted →
executing → done, plus owner + reviewer. The week's lived gap: specced
work was invisible outside relay logs (an ADR on a branch, a plan
uncommitted in a worktree, two doc PRs idle for days).
DATA: docs/{plans,adrs} frontmatter Status across repos (convention
already held); open PRs touching those paths (gh API); plans table
(submitted/executing); the migration roadmap's boulder index as the
curated overlay. Branch-only drafts are best-effort (orchestrator
branches pushed to origin are visible; uncommitted worktree files are
not — the ledger's existence pressures drafts onto pushed branches,
which is the desired behavior anyway).

### S5 — Loop activity feed (events as the spine)
Live feed of what the coordinators are routing: dispatches, verdicts,
merges, escalations, deploy/staging telemetry (observe-only), and the
ADR-0089 suppressed-wake digests. Filterable by repo/team/action.
This is v1's event list upgraded from audit-log to nervous-system view.
DATA: events table + the existing WS; wake-filter digests via the
channel server. Deploy/smoke rows deep-link via the `run_url` field
Carla's G3 workflows emit in the event payload ({repo, sha, digest,
env, services[], run_url}) — every telemetry row anchors to its actual
GitHub run/environment page, making the links-OUT principle concrete
per event rather than a generic repo link.

### S6 — Escalations (kept, vocabulary widened)
v1's escalation flow (open/ack/close + MTTR) survives intact; widen the
reason vocabulary to the ADR-0087 classes (evaluator_timeout,
rework-exhausted/max-cycles, mergeability_undetermined) and add the
op-readiness alert classes when they land (inference-silence,
dead-puller, never-fired validation gates).

## Kill list (remove from UI + API surface)
- Fleet/autoscaler panel + heartbeats (subject deleted).
- Review/corpus labeling queues (DSPy tables dropped in PR-F).
- Role/step pipeline rendering + any workflow_run-shaped assumptions
  (the PR-F one-step synthesis shims retire with S2).
- corpus_export surfaces (router already deleted).

## Boundary rule (hard, from the 2026-06-11 gate unwind)
No deploy/promotion CONTROLS in this UI, ever. Deploy + staging-smoke
telemetry renders observe-only (S5/S6); approval lives in GitHub
environment protection. The UI links OUT to the GitHub run/environment
page; it never hosts an approve button. (CLAUDE.md §System boundary.)

## Open questions for the design pass
1. S1+S5 composition: one "mission control" landing page or two
   screens? (Alan votes one landing — "is it alive" + "what is it
   doing" are one glance; Donna's lane pending, then decide.)
2. S3 granularity: is per-cycle cost a drill-down or front-and-center?
3. S4 cross-repo: one ledger with a repo facet, or per-repo tabs?
4. v1's operator buckets (blocked/inflight/hopper) — ANSWERED in
   sibling review (Carla, adopted): keep the buckets INSIDE S2 as the
   triage frame, nested with loop-stage facets. The buckets answer
   "is anything stuck?"; the stages answer "where is it stuck?" —
   different questions, both needed, nested not either/or.

## Non-goals
Story points (deferred); deploy controls (boundary); coordinator chat /
relay transcripts (the feed shows events, not conversation); multi-org.
