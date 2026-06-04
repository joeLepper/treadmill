# ADR-0073 — Persistent orchestrator sessions and interactive attach (systemd-user + tmux + cc-attach)

- **Status:** accepted
- **Date:** 2026-06-04
- **Related:** ADR-0067 (CC Channels, one bot per session), ADR-0068 (treadmill-events
  channel and shared channel conventions), ADR-0071 (relay log levels)

## Context

`tools/cc-channels/launch-session.sh` launches a labelled Claude Code session that supervises
Treadmill-dispatched work via the `treadmill-events` channel. It runs in the foreground of
whatever terminal the operator opened it in. Two problems emerge when sessions are meant to
be long-lived (days to weeks):

1. **No supervision.** If the terminal closes or the SSH connection drops, the Claude process
   dies and all in-flight work stalls. The operator has no way to reconnect.

2. **No interactive attach.** An operator who wants to inspect or intervene in a running
   session has no way to do so without opening a second Claude session (wrong: would create
   a conflicting session for the same label).

The desired model: a supervised background service per session label that auto-restarts on
failure, and a one-command attach (`cc-attach <label>`) that drops the operator into the
running session without creating a new one.

## Decision

Use **systemd user services + tmux** as the supervision and attach substrate.

1. **`treadmill-channel@.service`** — a parameterized systemd user unit (instance = label).
   Supervises the tmux-based launcher process; restarts on failure with a 5 s back-off.
   Installed into `~/.config/systemd/user/`.

2. **`treadmill-channel-launch <label>`** — the unit's `ExecStart` target. Idempotently
   creates/reuses a tmux session for the label, sends `launch-session.sh` into it, and then
   loops (`sleep 30`) while the tmux session exists, acting as the foreground process that
   systemd supervises. When the tmux session ends, the script exits and systemd may restart
   it.

3. **`cc-attach <label>`** — thin wrapper: `exec tmux a -t <label>`. Detach with the
   standard `Ctrl-b d`; the session and supervised claude process keep running.

4. **Single-instance guard** — `launch-session.sh` (the process running *inside* the tmux
   window) writes its PID to `~/.cc-channels/<label>/launcher.pid` before invoking Claude,
   and removes it on exit via `trap`. Both `treadmill-channel-launch` and
   `launch-session.sh` itself check this file: if a live PID is found, the new invocation
   refuses to start. This prevents duplicate sessions when:
   - systemd restarts `treadmill-channel-launch` while Claude is still running in the tmux
     session (the guard prevents sending a second launcher into the existing window); and
   - an operator invokes `launch-session.sh` directly, bypassing the supervised path.

## Diagram

```mermaid
sequenceDiagram
    participant Op as Operator
    participant SD as systemd --user
    participant CL as treadmill-channel-launch
    participant TM as tmux session <label>
    participant LS as launch-session.sh
    participant CC as claude (CC session)

    Op->>SD: systemctl --user enable --now treadmill-channel@bert.service
    SD->>CL: ExecStart treadmill-channel-launch bert
    CL->>CL: check launcher.pid → absent, proceed
    CL->>TM: tmux new -d -s bert (if absent)
    CL->>TM: send-keys "exec launch-session.sh --resume-by-label bert"
    TM->>LS: launch-session.sh --resume-by-label bert
    LS->>LS: check launcher.pid → absent; write $$
    LS->>CC: exec claude --channels ... (resumes per-label session-id)
    CL->>CL: loop: sleep 30 while tmux has-session -t bert

    Op->>Op: cc-attach bert
    Op->>TM: tmux a -t bert (attach)
    Note over Op,TM: Ctrl-b d to detach; CC keeps running

    CC-->>LS: (exits)
    LS->>LS: trap: rm launcher.pid
    TM-->>CL: tmux session ends
    CL-->>SD: exit 0
    SD->>CL: restart after 5 s (Restart=on-failure if exit ≠ 0)
```

## Alternatives considered

- **`nohup` / `screen`.** `nohup` gives no supervision or attach. `screen` is equivalent to
  tmux but less standard on modern systems. Rejected in favour of tmux, which the operator
  already uses.

- **systemd system service.** Would require `sudo`, would run as a system user (no `$HOME`
  channel state), and can't be `loginctl`-lingered. Rejected: user services are the right
  scope for per-operator workstation processes.

- **Docker / container supervision.** Correct for worker processes, wrong for operator
  sessions where interactive attach and a full workstation environment are required.

- **Daemon mode in `launch-session.sh` itself.** Would reinvent supervision, restart
  back-off, and start-on-login. systemd already does all of this; composing with it is the
  right choice.

## Consequences

### Good
- Sessions survive terminal close, SSH disconnect, and transient Claude crashes.
- `cc-attach <label>` is the entire operator interface for attaching to any supervised session.
- `loginctl enable-linger` means sessions start at boot and outlive login sessions.
- Standard tooling (`systemctl status/logs/stop`) covers supervision and troubleshooting.

### Bad / trade-offs
- **One-time setup per host:** `loginctl enable-linger`, installing the unit, installing
  `cc-attach` to PATH. Documented in `tools/cc-channels/README.md`.
- **tmux dependency.** Already present on most operator workstations; not assumed on
  worker hosts.
- **The `ExecStart` path is hardcoded** to `%h/treadmill/...`, assuming the repo is cloned
  at `~/treadmill`. The unit supports `TREADMILL_REPO_DIR` as an override via a
  systemd drop-in; documented in the README.

## Implementation

- **Step 1 (this ADR):** substrate — systemd unit, `treadmill-channel-launch`, `cc-attach`,
  PID-file guard in `launch-session.sh`, README runbook, PID-file unit test.
- **Step 2 (follow-on plan):** automated integration test that starts a supervised session
  against a stub Claude binary, verifies auto-restart, and validates `cc-attach` can
  attach/detach without killing the session.
