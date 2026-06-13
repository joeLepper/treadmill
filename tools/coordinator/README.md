# Coordinator (ADR-0084 + ADR-0086)

A coordinator is a long-lived Claude Code session that holds plan-level
context for a per-repo team. It briefs workers, routes SQS signals, and
maintains the team's task board. See ADR-0084 for the full design and
docs/plans/2026-06-08-adr-0084-coordinator-implementation.md for the
phased implementation.

The coordinator also owns the **per-task lifecycle** per
[ADR-0086](../../docs/adrs/0086-coordinator-owns-task-lifecycle.md).
Five mandatory responsibilities make the coordinator the canonical
writer of workflow_runs / workflow_run_steps / task_prs state:

1. **plan.submitted handler** — adds new plans to the in-memory
   `watched_plans` set + queries the new task board. Strictly
   in-memory: `coordinator.env` is read once at launch and never
   re-read; mutating it would not affect the running process.
2. **Pre-brief lifecycle registration** — POST /workflow_runs +
   PATCH step status="running" BEFORE the cc-relay brief lands. The
   dashboard reflects in-flight state before the worker sees the brief.
3. **PR registration** — parses `PR: #<number>` + `Branch: <name>`
   from the orchestrator's reply and POSTs /task_prs.
4. **Merge — two paths** — primary `github.pr_merged` webhook (Path
   A), 60s-timeout backstop via `gh pr view` + manual `POST /events`
   (Path B). Both routes PATCH the step to `completed`.
5. **Startup orphan recovery** — for every watched plan, query the
   API + reconcile the dashboard state against any work in flight
   while the coordinator was down.

See `coordinator_prompt.md` §12 for the full handler contracts; the
five lifecycle responsibilities are the bar for the lifecycle audit
LLM judge.

## Launch convention

Coordinator sessions ride the same systemd template as worker sessions
(`treadmill-channel@.service`). The distinguishing primitive is the
session label:

| Role        | Label pattern                | Workdir                                   |
|-------------|------------------------------|-------------------------------------------|
| Worker      | `treadmill-<name>`           | `~/treadmill` (or operator-supplied)      |
| Coordinator | `coordinator-<repo-slug>`    | `~/.treadmill/teams/<repo-slug>/`         |

The convention is enforced by `tools/cc-channels/launch-session.sh`:
when the label matches `coordinator-*`, the launcher pins the workdir
to the team directory and sources `coordinator.env` from there.

To start a coordinator for the `medicoder` repo:

```bash
systemctl --user start treadmill-channel@coordinator-medicoder.service
```

The unit will:

1. Create `~/.treadmill/teams/medicoder/` if absent.
2. Source `~/.treadmill/teams/medicoder/coordinator.env` if present.
3. Set the session workdir to `~/.treadmill/teams/medicoder/`.
4. Skip the dispatch-reminder print (coordinators route signals; they do
   not call `treadmill plan submit`).
5. Launch the Claude Code session with the standard treadmill-events
   channel, picking up `TREADMILL_COORDINATOR_PLANS` from the env file.

## coordinator.env

The conventional path is `~/.treadmill/teams/<repo-slug>/coordinator.env`.
The Treadmill API writes this file at plan-start with the assigned plan
UUIDs; the coordinator session reads it once at launch. A template lives
at `tools/coordinator/coordinator.env.template`.

Minimum contents:

```
TREADMILL_ROLE=coordinator
TREADMILL_COORDINATOR_PLANS=<comma-separated-plan-uuids>
```

`TREADMILL_COORDINATOR_PLANS` widens the treadmill-events WebSocket
subscription beyond the coordinator's own `created_by=<label>` work to
include events for every task in those plans — that is what lets one
coordinator session route signals for many workers.

### v1 limitation: startup-only subscription

The coordinator reads `coordinator.env` exactly once, when the session
launches. If the plan set changes while the coordinator is running
(a new plan is assigned, or an existing one finishes and is removed),
the API must restart the unit:

```bash
systemctl --user restart treadmill-channel@coordinator-<repo>.service
```

A live-reload mechanism (file watch, dynamic re-subscribe, or a small
coordinator-side API) is deferred to v2. See ADR-0084 §3A risk note.

## Team directory layout

```
~/.treadmill/teams/<repo-slug>/
├── coordinator.env       # API-written; sourced at launch
├── task_board.sqlite     # coordinator's working overlay (planned, Task 3B+)
└── memory/               # per-repo memory (planned, Task 3B+)
    └── main.md
```

The team directory IS the coordinator's workdir; everything the
coordinator reads or writes is rooted here. Per-Claude worktrees and the
coordinator team directory are independent — the worktree convention
(e.g. `/home/joe/treadmill-worktrees/treadmill-<name>`) is for
*editing* the Treadmill repo across parallel sessions; the team dir is
for *operating* a coordinator session.

## Phase 5 launch

The Phase 5 end-to-end proof runs against RAMJAC (medicoder). Three
pieces are pre-staged for one-command launch:

1. A specialized systemd unit at
   `tools/cc-channels/systemd/treadmill-channel@coordinator-medicoder.service`
   — sets `TREADMILL_ROLE=coordinator`, `TREADMILL_LABEL=coordinator-medicoder`,
   `EnvironmentFile=%h/.treadmill/teams/medicoder/coordinator.env` (the
   `-` prefix tolerates a missing file), `WorkingDirectory=%h/.treadmill/teams/medicoder`.
2. The `launch-session.sh` label-detection from Task 3A — also sources
   `coordinator.env` and pins workdir, so the two layers agree even when
   the unit is bypassed (direct `launch-session.sh coordinator-medicoder`
   on the operator's terminal).
3. A wrapper `tools/coordinator/launch-coordinator.sh` that reconciles
   the plan id into the env file and starts the unit.

### Installing the unit (one-time per workstation)

```bash
mkdir -p ~/.config/systemd/user
ln -s ~/treadmill/tools/cc-channels/systemd/treadmill-channel@coordinator-medicoder.service \
    ~/.config/systemd/user/treadmill-channel@coordinator-medicoder.service
systemctl --user daemon-reload
```

The unit name uses `@coordinator-medicoder` literally — systemd resolves
this as a concrete unit name and uses the specialized file (not the
generic `treadmill-channel@.service` template).

### Starting a coordinator for a plan

```bash
~/treadmill/tools/coordinator/launch-coordinator.sh \
    --repo medicoder --plan-id <plan-uuid>
```

The wrapper:
- Ensures `~/.treadmill/teams/medicoder/` exists.
- Reconciles `coordinator.env` — replaces an existing
  `TREADMILL_COORDINATOR_PLANS=` line if present (preserving any other
  vars the API wrote, e.g. `TREADMILL_OPERATOR_INSTANCE`), or writes
  `TREADMILL_ROLE` + the plan id if the file is fresh.
- Runs `systemctl --user start treadmill-channel@coordinator-medicoder.service`.

### Observing the session

```bash
tmux attach -t coordinator-medicoder
```

systemd's `Restart=on-failure` brings the unit back if the session
crashes (Claude OOM, tmux server died), restoring the tmux session
behind the same label. The Phase 5 quality gate (≥10 tasks brokered,
amend rate ≤ 30%) is measured across the session's plan-close events.

### Stopping cleanly

```bash
systemctl --user stop treadmill-channel@coordinator-medicoder.service
```

The launcher sets a SIGTERM/SIGINT trap that suppresses systemd's
restart-on-failure when stop is operator-initiated. An unexpected
session end still triggers restart.

### Adding a new repo

Copy `treadmill-channel@coordinator-medicoder.service` and substitute
every `medicoder` with the new slug. Same install-via-symlink, same
launch wrapper with `--repo <new-slug>`.

## Worker availability protocol

Workers don't wait to be asked — they announce when they're idle.

The mechanism: `tools/dev-hooks/broadcast-idle.py` runs as a `Stop`
hook (`.claude/settings.json`) when a worker session finishes
responding. It writes an `[AVAILABLE]` relay file into the OWNING
coordinator's inbox (`~/.cc-channels/coordinator-<slug>/relay/`) plus
an availability record at `~/.treadmill/availability/<label>.json`.

Only `worker-<slug>-<n>` labels broadcast. Orchestrators
(`treadmill-*`), coordinators (`coordinator-*`), evaluators
(`evaluator-*`), and any other label class are skipped — they are not
coordinator-routed workers and their idle ticks would wake every
coordinator with no actionable signal (task b71be765: 20+ spurious
wakes observed from orchestrator idle ticks).

The owning coordinator is derived by string surgery — not a positional
split (owner/repo slugs contain hyphens): strip `worker-` prefix, strip
trailing `-<digits>` index, prepend `coordinator-`. If the owning
coordinator's inbox doesn't exist, the relay write is suppressed (no
fan-out fallback); a missed assignment self-heals on the next cooldown
tick.

The relay file shape:

```
[AVAILABLE]

[from: worker-medicoder-1]

Worker worker-medicoder-1 is idle and available for task assignment.
```

Filename convention: `<ns_ts>-<token>-available-from-<label>.md` so
two workers idling in the same nanosecond don't collide.

**Cooldown**: 3600 seconds per worker (tracked in
`~/.treadmill/session-state/<label>/last-idle-broadcast`). A worker
that finishes 12 turns in 5 minutes broadcasts once, not 12 times.

**Coordinator side**: the prompt §4 routing table handles `[AVAILABLE]`
relays as a routing opportunity — check the task board for `ready`
tasks and brief the worker, or no-op if nothing is queued.

**Skip conditions** (the hook returns 0 silently):
- `TREADMILL_SESSION_LABEL` unset — not a labeled session.
- Label is not `worker-<slug>-<n>` shape — non-workers don't broadcast.
- Cooldown still active (< 3600s since last broadcast).
- Owning coordinator inbox missing — relay suppressed, no fan-out.
- I/O failures (disk full, permission denied) — Stop hooks must not
  block the worker from idling.

**Wiring**: every worker session running in the Treadmill repo (or any
of its `.claude/settings.json`-scoped worktrees) gets the hook
automatically. The hook is in the project settings file; no per-worker
configuration is required.

## Future contents

This directory will grow to hold the coordinator's operating prompt and
helper scripts as Task 3B + 3C + 3D land:

- `coordinator_prompt.md` — system prompt covering brief format, signal
  routing table, escalation chain, self-compaction guidance.
- `brief_worker.py` — task-brief templating helper.
- `handoff/` — handoff-doc generator + receiver prompt fragments.
