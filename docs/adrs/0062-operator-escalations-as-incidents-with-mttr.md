# ADR-0062 — Operator escalations as incidents with MTTR tracking and notification fan-out

- **Status:** proposed
- **Date:** 2026-05-28
- **Supersedes:** none
- **Related:** ADR-0048 (operator escalation event), ADR-0058
  (architect gate-broken verdict — one of the escalation producers),
  ADR-0035 (scheduler — drives the close-detection sweep), ADR-0056
  (operator dashboard — existing rendering surface)

## Context

`task.escalated_to_operator` events fire correctly from five producer
sites today:

- architect-cap (3 amend rounds exhausted)
- stuck-task-sweep (no downstream dispatch in N minutes)
- wf-conflict-cap-reached (merge-conflict resolver gave up)
- wf-ci-fix-cap-reached (CI-fix loop gave up)
- gate-broken (architect's 4th verdict, ADR-0058)

Producers work. **Consumers don't.** The only existing surface is the
dashboard's `_ESCALATIONS_SQL` view in `routers/dashboard/overview.py`,
which renders open escalations on the operator's dashboard if they
happen to look. There is no real-time notification: the operator
sees the escalation only when they explicitly check.

The 2026-05-28 evening session demonstrated the failure mode end to
end. Task `a35f3c79` (ADR-0060 Step 3c) hit
`wf-conflict-cap-reached` at 23:28 UTC and emitted
`escalated_to_operator`. The orchestrator on duty (me) was running a
`plan show` poll every ~15 min, saw `wf-conflict: executing`, and
declared the task healthy. The escalation row sat untouched for
20 minutes until the human operator (Joe) noticed and prompted a
deeper look. The same evening produced 7 escalation events across 5
distinct tasks, 3 from concurrent operators all stalling on the
same `workers/agent/AGENT.md` "Recent changes" hotspot — none
surfaced beyond the dashboard.

A second gap: even after I resolved the conflict (rebased + force-
pushed + the PR merged), there is no signal in the event stream
that says "this incident is closed." The system still treats the
task as escalated until an explicit `TaskEscalationAcknowledged`
event lands — and even ack only suppresses the dashboard row; it
doesn't capture *when* the incident was actually resolved or how
long it took.

**The reframe:** escalations are the **beginning of an incident**, not
a static flag. The right model is the o11y incident lifecycle —
open at the escalation event, closed at the resolution event, with
MTTR computed across the pair. Real-time notification fires on open
(so the operator sees it within seconds, not 20 minutes), and the
close signal is what makes the MTTR distribution observable over
time.

The existing `TaskEscalationAcknowledged` event is a "operator saw
it" signal — useful for suppressing further notifications — but it
is not the same as "incident resolved." A wave of acked-but-
unresolved incidents looks healthy on the dashboard while MTTR is
silently growing. We need both signals, distinct.

## Decision

Layer an **Incident model** on top of the existing escalation event
stream. No new tables — the model is event-sourced, consistent with
the rest of `coordination/`.

### Events

| Event | When | Payload |
|---|---|---|
| `task.escalated_to_operator` | Producer fires (architect-cap, stuck-task-sweep, wf-conflict-cap, wf-ci-fix-cap, gate-broken) — **unchanged** | reason, last_verdict, gate_log_excerpt |
| `task.escalation_acknowledged` | Operator acks via UI / CLI — **unchanged** | empty |
| `task.escalation_closed` | **NEW** — incident resolved | close_reason, opened_at (denormalized), mttr_seconds |

A task's incident state is derived by joining the three event types
on `task_id`, ordered by `created_at`. The most recent
`escalated_to_operator` opens the incident; the next
`escalation_closed` after that timestamp closes it; intervening
`escalation_acknowledged` rows mark the operator-saw-it transition
but do not close the incident.

A task can have multiple sequential incidents (escalate → close →
escalate again). The model handles this naturally: open / close are
paired by `created_at` ordering within the task's event tail.

### Close detection

A periodic sweep (mirrors `stuck_task_sweep`'s scheduler hook) emits
`escalation_closed` for any open incident whose underlying task
satisfies a close trigger:

| Close trigger | Detection | `close_reason` |
|---|---|---|
| Task re-progressed | A `step.completed` event with `created_at > incident.opened_at` exists for the task | `re_progressed` |
| Task terminal — merged | A `pr_merged` event for the task exists | `pr_merged` |
| Task terminal — cancelled | A `task.cancelled` event for the task exists | `cancelled` |
| Task terminal — superseded | A `task.superseded` event for the task exists | `superseded` |
| Operator explicit close | `treadmill escalations close <task_id>` CLI / equivalent UI button | `operator_close` |

The sweep runs on a `*/2` schedule (every 2 minutes — escalations
are infrequent enough that the sweep cost is negligible; latency
matters more than throughput). MTTR is `closed_at - opened_at`
recorded on the `escalation_closed` payload.

The 2026-05-28 incident above would close as `re_progressed`
within 2 minutes of the next sweep after the conflict-resolving
PR's `step.completed` reached the workflow.

### Notification fan-out

A new in-process subscriber on the events table consumes new
`escalated_to_operator` and `escalation_closed` events and posts
to a configurable list of notification targets. The subscriber
runs in the API process (same model as `coordination/consumer.py`).

**Primary target: Slack webhook.** A single environment variable
`TREADMILL_SLACK_WEBHOOK_URL` configures the channel; the
subscriber POSTs a small JSON blob on each event:

- Open: "🚨 escalation: `task.id[:8]` `task.title` — reason
  `wf-conflict-cap-reached` — opened 0s ago — see dashboard /
  CLI"
- Close: "✅ closed: `task.id[:8]` — reason `re_progressed` —
  MTTR 2m17s"

The MTTR appearing on every close makes the channel a self-
documenting incident log; aggregate MTTR over a window is a
trivial channel-export operation.

**Pluggable fan-out:** The subscriber accepts a list of webhook
URLs (env: `TREADMILL_NOTIFICATION_WEBHOOKS`, comma-separated)
plus an explicit Slack channel for the formatted-for-Slack
target. Any URL in the list receives the raw event JSON via POST.
Discord, Mattermost, ntfy.sh, custom internal services all work
via this surface without code changes.

### CLI surface

A new `treadmill escalations` command group:

| Command | Purpose |
|---|---|
| `treadmill escalations tail` | Stream new open / close events; matches `gh run watch` cadence (≤5s latency) |
| `treadmill escalations list [--open] [--reason <r>] [--task <id-prefix>]` | Point-in-time inbox |
| `treadmill escalations close <task_id> [--reason <text>]` | Emits `escalation_closed` with `close_reason=operator_close` |
| `treadmill escalations ack <task_id>` | Existing ack path, surfaced in the CLI (currently dashboard-only) |
| `treadmill escalations report [--since <date>] [--by reason\|operator\|day]` | MTTR aggregation report — count, p50, p95, max per bucket |

The `tail` command is what closes the loop for an active
orchestrator: an agent doing long-running work runs `escalations
tail` in a background scrollback and sees its own escalations the
moment they fire — no polling discipline required.

## Notification surface alternatives considered

Listed for completeness; rejected for the reasons given.

| Alternative | Why considered | Why rejected (or deferred) |
|---|---|---|
| **Slack webhook** (recommended primary) | Operator already runs Slack; webhook is one POST; channel becomes a visible MTTR-tracking log automatically | — |
| **Email (SES)** | Reliable, persistent, archivable | Latency too high for fast-moving ops (minutes vs seconds); inbox clutter; deferred to MTTR report digests |
| **Push via ntfy.sh / Pushover** | Mobile-friendly, simple webhook | Covered by pluggable webhook fan-out — operator adds the ntfy URL to `TREADMILL_NOTIFICATION_WEBHOOKS` |
| **GitHub issue per escalation** | Built-in tracking | Pollutes the issue tracker; conflates "operational incident" with "engineering work item" |
| **PagerDuty / OpsGenie** | Real on-call rotation | Overkill for current solo-plus-a-few-orchestrators ops scale; revisit when the team grows |
| **Mobile push directly (APNs / FCM)** | Native experience | Requires building/operating an app + push infrastructure; covered acceptably by Slack mobile or ntfy |
| **Discord webhook** | Similar idiom to Slack | Covered by pluggable fan-out |

The pluggable webhook fan-out makes the choice between targets a
configuration concern rather than an architectural one — Slack is
the recommended *primary* because it gives the best out-of-the-box
visibility for our current setup, but the architecture doesn't lock
us in.

## Consequences

**Positive:**

- Operator (human or agent) finds out about an escalation within
  seconds, not the next time they happen to check. The 20-minute
  silent-stall pattern from 2026-05-28 becomes a sub-minute
  notification.
- MTTR becomes a first-class observability metric. A regression in
  MTTR distribution is the signal that says "the architect's
  retry-cap is firing more often" or "wf-conflict's auto-resolver
  is hitting cap more often" before anyone manually counts.
- The close-detection sweep turns the ad-hoc "is this resolved?"
  human-judgment moment into a deterministic event. Backfills the
  audit trail for incidents already-resolved-but-still-marked-open.
- Slack channel becomes a self-documenting incident log without
  separate tooling.
- Pluggable webhook fan-out keeps the architecture open to future
  notification targets (Discord, ntfy, custom internal services)
  without code changes.

**Negative:**

- A new periodic sweep adds a tick to the scheduler workload.
  Mitigation: scope is tiny (open-incidents-only query + a handful
  of close-trigger lookups per incident); `*/2` cadence is below
  any reasonable scheduler-overhead concern.
- A Slack webhook URL is a secret that lives in env config; rotation
  / leakage is a real concern. Mitigation: same secrets channel as
  `CLAUDE_CODE_OAUTH_TOKEN` per ADR-0055; rotate per established ops
  procedure.
- The fan-out subscriber adds a path that can silently fail (network
  to Slack down, webhook URL stale). Mitigation: subscriber failures
  log to stderr / OTel; the event store is the source of truth
  regardless of whether the notification fired.

**Neutral:**

- `TaskEscalationAcknowledged` stays — the ack model is preserved
  as a distinct "operator saw it, suppress further notifications"
  signal, separate from "incident resolved." A future iteration may
  collapse them if usage patterns show ack and close almost always
  fire together, but for now both signals are useful.
- The dashboard's `_ESCALATIONS_SQL` view is updated to read the
  new `escalation_closed` event, but the surface is unchanged from
  the operator's perspective — closed incidents stop appearing in
  the open-incidents list as soon as the close event lands.

## Sequence (high-level — full step list in the plan)

1. **Event + sweep.** Add `TaskEscalationClosed` event payload;
   register in `events/registry.py`; add the close-detection sweep
   to `coordination/` with a `*/2` schedule entry. The sweep runs
   the five close-trigger queries against open incidents and emits
   the close event with MTTR computed. Unit tests cover each close
   trigger.
2. **CLI surface.** New `treadmill escalations` group: `tail`,
   `list`, `close`, `ack`, `report`. Reuses the existing API
   client; the `tail` command long-polls a streaming endpoint
   added on the API side.
3. **Slack notifier.** In-process subscriber on the events stream;
   posts to `TREADMILL_SLACK_WEBHOOK_URL` on each open / close
   event. Slack-specific formatting (emoji, link to dashboard).
4. **Pluggable webhook fan-out.** Generalize the notifier to a list
   of webhook URLs from `TREADMILL_NOTIFICATION_WEBHOOKS`; raw
   event JSON for non-Slack targets.
5. **Dashboard integration.** Update `_ESCALATIONS_SQL` to honor
   the new close event; add a per-incident MTTR column to the
   dashboard's escalations table.
6. **MTTR report.** Server-side aggregation endpoint + the CLI
   `report` subcommand. Surface to Grafana later via OTel metrics
   if a real trend matters.

## Alternatives considered (architecture)

**Add an `incidents` table** instead of event-sourcing. Rejected for
consistency with the rest of `coordination/`, which is event-sourced
end to end. The view-layer queries to derive open / closed state
from events are cheap (small joined window per task) and the
event-sourced model gives us replayable history for free.

**Collapse ack and close into one event.** Considered for
simplicity, rejected because they signal different things: ack is
"I've seen it" (notification suppression), close is "the underlying
condition is resolved" (MTTR endpoint). A future iteration may
collapse if usage shows the distinction doesn't matter in practice.

**Push-notification-only (no MTTR tracking).** The simpler version
of this ADR. Rejected because the o11y framing is the durable win
— a Slack channel without MTTR is just a notification log; with
MTTR it becomes an SLO surface.

**Have the producers compute MTTR at close time, not the sweep.**
Each producer would need to know how to detect resolution for its
own escalation reason — couples close detection to producer
authoring. Rejected; the centralized sweep keeps close detection
in one place.
