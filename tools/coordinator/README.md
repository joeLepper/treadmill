# Coordinator (ADR-0084)

A coordinator is a long-lived Claude Code session that holds plan-level
context for a per-repo team. It briefs workers, routes SQS signals, and
maintains the team's task board. See ADR-0084 for the full design and
docs/plans/2026-06-08-adr-0084-coordinator-implementation.md for the
phased implementation.

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

## Future contents

This directory will grow to hold the coordinator's operating prompt and
helper scripts as Task 3B + 3C + 3D land:

- `coordinator_prompt.md` — system prompt covering brief format, signal
  routing table, escalation chain, self-compaction guidance.
- `brief_worker.py` — task-brief templating helper.
- `handoff/` — handoff-doc generator + receiver prompt fragments.
