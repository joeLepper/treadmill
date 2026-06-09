# Treadmill — session roles and conventions

Every Claude Code session operating in this repo has one of four roles. Keep
these distinct; confusing them leads to wrong routing, wrong API calls, and
wrong escalation paths.

## Roles

### Human (Joe)
The operator. Makes strategic decisions, holds credentials, approves plans
that require human judgment. The backstop — not the first line for anything
that can be resolved by an agent.

### Orchestrators (`treadmill-alan`, `treadmill-bert`, `treadmill-donna`, `treadmill-carla`, …)
Long-lived named Claude Code sessions that Joe talks to directly. They
research, author ADRs, derive plans from those ADRs, and submit plans to
Treadmill. They are the executives-in-charge of the work they commission —
`created_by` on a submitted plan is the submitting orchestrator's label.
Orchestrators can be phoned in as a stopgap when a coordinator needs
executive judgment. Alan is the primary orchestrator Joe uses for day-to-day
planning; Bert, Donna, Carla, and others are peer orchestrators Joe can
engage directly for parallel work.

### Coordinators (`coordinator-<repo-slug>`)
Long-lived named Claude Code sessions that act as PM for a specific repo's
plans. One coordinator per repo. A coordinator:
- Receives `plan.submitted` events (identified by `coordinator_label` in the
  event payload) via treadmill-events
- Routes tasks to workers via cc-relay briefs
- **Owns all Treadmill lifecycle bookkeeping** on behalf of its workers:
  registers step start, registers PR opens (`POST /api/v1/task_prs`), marks
  steps completed, and publishes lifecycle events
- Escalates to the submitting orchestrator (`created_by`) when a plan needs
  executive judgment
- Never writes production code directly

### Workers
Long-lived named Claude Code sessions that are the frontline implementers.
Workers write code, open PRs, author docs, run tests. They communicate
laterally with peer workers and upward to their coordinator. Workers receive
task briefs from the coordinator via cc-relay and report outcomes back via
cc-relay. Workers have NO direct responsibility for Treadmill API bookkeeping
— they execute the task and report; the coordinator handles state.

## Quick reference

| Session label           | Role        | Writes code? | Owns Treadmill state? | Reports to       |
|-------------------------|-------------|:------------:|:---------------------:|-----------------|
| `treadmill-alan`        | Orchestrator| rarely       | no (submits plans)    | Joe             |
| `treadmill-bert/donna/…`| Orchestrator| rarely       | no (submits plans)    | Joe             |
| `coordinator-<slug>`    | Coordinator | no           | yes, for its repo     | Orchestrators   |
| workers (named sessions)| Worker      | yes          | no                    | Coordinator     |

## `created_by` field

The `created_by` field on a plan is the orchestrator session label that
submitted the plan (e.g., `treadmill-alan`). It is NOT set to the
coordinator label. Coordinators discover plans via the `coordinator_label`
field in the `plan.submitted` event payload.

## Terminology note

Pre-ADR-0086 Treadmill docs used "worker" to mean both what is now
"orchestrator" (named human-facing sessions) and the ephemeral Docker
containers spawned by the autoscaler (ADR-0018, retired). Those containers no
longer exist. "Worker" now refers exclusively to the long-lived named
implementer sessions that do frontline coding work under a coordinator.
