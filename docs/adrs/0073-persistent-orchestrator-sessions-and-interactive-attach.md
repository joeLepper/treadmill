# ADR-0073: Persistent orchestrator sessions and interactive attach

- **Status:** accepted
- **Date:** 2026-06-04
- **Related:** ADR-0067 (Claude Code phone-access channels), ADR-0068 (treadmill-events
  channel as the in-session event bus), ADR-0071 (operator notification strategy)

## Context

Labelled Claude Code orchestrator sessions are the operator's primary surface for
away-from-keyboard work (ADR-0067/0068): one persistent session per label, paired with
its own Telegram bot and `treadmill-events` channel, resumed across launches via the
`session-id` file under `~/.cc-channels/<label>/`. That state file already gives us
**identity** across restarts — `launch-session.sh <label>` always re-enters the same
Claude Code conversation.

What it does not give us is **liveness**. Today the operator launches the session in
whatever terminal they happen to be at; closing the terminal kills the session, and
re-launching mints a fresh process even though the conversation resumes. Two concrete
failure modes follow:

- **Loss on disconnect.** SSH drops, laptop sleep, terminal close — the session ends.
  The operator notices later by tailing logs or pinging the bot. The phone bot only
  responds while the session is alive, so a session that quietly died looks identical
  to one that's idle.
- **Accidental duplicate launches.** Two terminals running `launch-session.sh bert`
  spawn two Claude Code processes both claiming the same label. The Telegram plugin's
  `bot.pid` singleton guard catches some of this, but only after both have raced to
  bind to the same state dir; the second often kills the first or wedges. There is no
  upstream guard that says "this label already has a running launcher."

We need (a) a supervisor that keeps exactly one launcher alive per label across
disconnects, reboots, and crashes, and (b) an attach interface so the operator can
peek at — and drop into — a running session without restarting it. The substrate has
to work without privileged services (the operator does not have a Treadmill-managed
daemon to talk to; their orchestrator host is whatever Linux box they ssh into).

## Decision

We adopted **systemd-user + tmux + a `cc-attach` thin wrapper** as the supervision
substrate for orchestrator sessions:

1. **systemd-user unit `treadmill-channel@<label>.service`** owns liveness. The unit
   is templated on label; one instance per label. `loginctl enable-linger` keeps it
   alive across logout. `Restart=on-failure` handles crashes. The unit invokes a
   small launch wrapper, not Claude directly.

2. **tmux session named `<label>`** owns the TTY. The wrapper creates the tmux session
   if missing and sends `launch-session.sh <label>` into it. Putting Claude inside
   tmux is what makes interactive attach possible: the operator's terminal and the
   supervisor's terminal can both watch the same PTY.

3. **`cc-attach <label>`** is a one-line wrapper around `tmux a -t <label>` — the
   only operator-facing command. Detach (`Ctrl-b d`) returns to the shell without
   killing anything. Stopping the session goes through `systemctl --user stop`, not
   the terminal.

4. **Single-instance contract.** Both the wrapper and `launch-session.sh` check
   `~/.cc-channels/<label>/launcher.pid` and refuse to start if a live PID is already
   recorded. The wrapper's check defends against operator races; the launcher-internal
   check defends against an operator bypassing the wrapper with a direct invocation.

The unit references the launcher via `${TREADMILL_REPO_DIR:-$HOME/treadmill}` so the
operator can override the clone location without editing the unit file.

## Alternatives considered

- **A purpose-built daemon (e.g. systemd-managed Python supervisor).** Rejected —
  we would be reimplementing what systemd-user already gives us (restart policy,
  per-instance templating, lifecycle commands) and adding an attach UI on top of it.
  The whole point of tmux here is that the attach UX is solved.
- **`screen` instead of `tmux`.** Rejected on familiarity grounds; tmux is already on
  every box we develop against and its detach/attach idioms are the ones operators
  know. No strong technical reason against `screen`.
- **`nohup launch-session.sh &` + log tail.** Rejected — no restart on crash, no
  attach (only a log tail, can't type back), no singleton guard.
- **Containerize the session.** Rejected for this step — would require packaging Claude
  Code's host-side state (`~/.claude/`, plugins, MCP servers) into an image, which is
  a large project and orthogonal to the supervision problem.
- **Resolve `--resume-by-label` as a launcher flag.** Rejected — `launch-session.sh`
  already reads `~/.cc-channels/<label>/session-id` from the label-positional argument
  and resumes automatically. Adding a flag would be redundant surface area.

## Diagram

```mermaid
flowchart LR
    SD[systemd --user] -- ExecStart --> WRAP[treadmill-channel-launch label]
    WRAP -- create-if-missing --> TMUX[tmux session name=label]
    WRAP -- send-keys --> TMUX
    TMUX -- runs --> LAUNCH[launch-session.sh label]
    LAUNCH -- writes pid --> PIDFILE[~/.cc-channels/label/launcher.pid]
    LAUNCH -- exec --> CLAUDE[claude --resume sessionId]
    OP[Operator] -- cc-attach label --> TMUX
    OP -- Ctrl-b d --> OP
    OP -- systemctl --user stop --> SD
```

## Consequences

### Good
- One stable session per label that survives SSH drops, terminal closes, and crashes
  without operator intervention.
- Attach/detach without restart — the conversation, MCP connections, and channel
  state all persist across operator presence changes.
- No new daemon to write, package, or supervise; the substrate is OS-native.
- Singleton enforcement happens at two layers (wrapper + launcher), so an operator
  who bypasses the wrapper still cannot race themselves.

### Bad / trade-offs
- Adds three new operator-facing concepts (systemd unit, tmux session, attach wrapper)
  to a directory that previously had one (the launcher). Mitigated by the README
  install runbook, which is short.
- The unit's `ExecStart` path bakes in a clone-location assumption
  (`$HOME/treadmill`). Operators who clone elsewhere set `TREADMILL_REPO_DIR`; the
  README documents this.
- A stale `launcher.pid` (e.g. machine power-cut mid-write) is detected by the
  `kill -0` check and cleaned up on next start, but a PID-reuse collision in the
  ~minute between a kernel panic and reboot would falsely report "already alive."
  Judged acceptable; the operator's recovery path (`rm` the file, `systemctl start`)
  is documented.

### Risks
- Claude Code's CLI contract for `--resume <session-id>` is what makes the persistent
  session work; a future CC release that changes this would break the substrate.
  ADR-0067/0068 already track this as a research-preview risk.
- `loginctl enable-linger` requires the operator be a real user (uid ≥ 1000 with a
  systemd-user instance). On hosts where that isn't true (some container substrates),
  the operator falls back to running the launcher directly without supervision; the
  attach UX is unavailable. We accept this; the supervisor exists for the long-lived
  operator-host case, not for ephemeral CI runners.

## Follow-ups

- **Step 2 (separate ADR + plan):** automated coverage. The PID-file refusal is unit-
  testable today; the systemd/tmux orchestration is operator-smoke-tested by design.
  A future plan will introduce a container-based integration test once the substrate
  has soaked.
- **`cc-status <label>`** as a sibling wrapper — `systemctl --user status` + tmux
  `list-sessions` in one line. Deferred until the substrate has soaked in operation.
