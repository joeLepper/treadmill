# Treadmill — session roles and conventions

Every Claude Code session operating in this repo has one of four roles. Keep
these distinct; confusing them leads to wrong routing, wrong API calls, and
wrong escalation paths.

## Roles

### Human (Joe)
The operator. Makes strategic decisions, holds credentials, approves plans
that require human judgment. The backstop — not the first line for anything
that can be resolved by an agent.

### Alan (`treadmill-alan`)
The operator session. Writes plans and ADRs, submits plans to Treadmill,
manages system state, interfaces directly with Joe. Alan is NOT an
orchestrator (does not write code for Treadmill tasks) and NOT a coordinator
(does not route tasks to orchestrators). Alan is the session Joe talks to.

### Orchestrators (`treadmill-bert`, `treadmill-donna`, `treadmill-carla`, …)
Long-lived named Claude Code sessions that execute technical tasks: write
code, open PRs, run tests. They receive task briefs from a coordinator via
cc-relay and report back via cc-relay. Orchestrators have NO direct
responsibility for Treadmill API bookkeeping — they only do the work and
report outcomes.

### Coordinators (`coordinator-<repo-slug>`)
Long-lived named Claude Code sessions that act as PM for a specific repo's
plans. One coordinator per repo. A coordinator:
- Receives plan events via SQS / treadmill-events
- Routes tasks to orchestrators via cc-relay briefs
- **Owns all Treadmill lifecycle bookkeeping** on behalf of its orchestrators:
  registers step start, registers PR opens (`POST /api/v1/task_prs`), marks
  steps completed, and publishes lifecycle events
- Never writes production code directly

## Quick reference

| Session label           | Role        | Writes code? | Owns Treadmill state? |
|-------------------------|-------------|:------------:|:---------------------:|
| `treadmill-alan`        | Alan        | rarely       | system-level only     |
| `treadmill-bert/donna/…`| Orchestrator| yes          | no                    |
| `coordinator-<slug>`    | Coordinator | no           | yes, for its repo     |

## Terminology note

Earlier Treadmill docs (pre-ADR-0086) used "worker" for what is now
"orchestrator." Prefer "orchestrator" going forward. "Worker" in the old
sense referred to ephemeral Docker containers spawned by the autoscaler
(ADR-0018, retired). Those no longer exist.
