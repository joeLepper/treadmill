# Treadmill — session roles and conventions

Every Claude Code session operating in this repo has one of four roles. Keep
these distinct; confusing them leads to wrong routing, wrong API calls, and
wrong escalation paths.

## Messaging — the harness peer channel (fabric retired)

Inter-session messaging runs on the **Claude Code harness peer channel**, NOT the
exec_otp fabric. The **exec_otp fabric** (`send <agent>` / `agentctl`) is RETIRED
(operator directive 2026-10-01); do not use it.

- **Claude ↔ Claude siblings** (alan, bert, carla, donna, ernie, the coordinator /
  workers): use the **`SendMessage`** tool; discover live peers with **`ListAgents`**
  (transport is local cc-socks sockets). A send reaches the session, not necessarily
  its operator — a peer in a stricter permission mode may hold the message for approval.
- **Claude ↔ Fran** (the **Codex** sibling): Fran runs `codex` and is OFF the harness
  peer bus (no stable `fran` address). Reach her through the **Codex bridge session** — the
  `cxbridge` / `bridge-session-*` sender, which registers in `ListAgents` as "Fran-bridge
  online". Its address **rotates on restart**, so resolve the current `cxbridge` row via
  `ListAgents` each session, then **`SendMessage`** to it (a `For Fran:` routing prefix is
  the convention). The bridge relays to Fran; her reply comes back as a cross-session message
  from the bridge, body prefixed **`[from Fran]`**. Caveat: the bridge relays only what Fran
  emits to its outbox — it does NOT scrape her terminal, so if no reply comes back, re-send.
  (Verified by the Codex passes on 2026-10-01, Carla + Donna.)
- **cc-relay** (`tools/cc-channels/cc-relay.py`, skill retained): a legacy file-drop
  transport. It is NOT the current Fran bridge path (tonight's passes used `SendMessage`
  to the bridge). Keep the tool; prefer the paths above.

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
  event payload)
- Routes tasks to its workers
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
task briefs from the coordinator and report outcomes back. Workers have NO
direct responsibility for Treadmill API bookkeeping
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

## System boundary

Treadmill orchestrates teams: plans, tasks, reviews, lifecycle events,
escalations. It does NOT control other repos' deploy mechanics — deploy
approval, promotion, and rollback belong to each repo's own CI (e.g.
GitHub environment protection). If Treadmill ever acts on a deploy, it
does so as the operator through the repo's own gate (PAT / gh CLI on the
operator's behalf), never via a parallel approval system. (Operator
directive 2026-06-11; see
docs/learnings/2026-06-11-check-the-incumbent-before-designing.md.)

## Terminology note

Pre-ADR-0086 Treadmill docs used "worker" to mean both what is now
"orchestrator" (named human-facing sessions) and the ephemeral Docker
containers spawned by the autoscaler (ADR-0018, retired). Those containers no
longer exist. "Worker" now refers exclusively to the long-lived named
implementer sessions that do frontline coding work under a coordinator.
