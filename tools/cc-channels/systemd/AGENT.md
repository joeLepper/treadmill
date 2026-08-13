# tools/cc-channels/systemd

## Purpose

Systemd-user units and wrapper scripts that supervise Treadmill orchestrator
sessions under ADR-0073. One `treadmill-channel@<label>.service` instance per
label; the service's `ExecStart` is `treadmill-channel-launch`, which creates a
tmux session if absent, sends the Claude launcher into it, and stays alive in
the foreground so systemd tracks liveness. `ExecStopPost` runs
`treadmill-channel-reap` to tear down the label's tmux tree on every stop —
the fix for the 2026-06-11 orphan crashloop (NRestarts=4119; the tree is
parented to the tmux server's cgroup, invisible to `KillMode`).

## Key surfaces

- `treadmill-channel@.service` — unit template; one instance per label.
  `Restart=on-failure`, `RestartSec=5s`, `SuccessExitStatus=143` (intentional
  stop exits 143 via the TERM trap; without this, every scheduled pause lands
  in `failed` until `reset-failed`).
- `treadmill-channel-launch` — foreground wrapper invoked by the unit. Creates
  the tmux session and sends the launcher into it, polls for startup prompts
  (dev-channels gate, workspace-trust, stale limit modal), then loops 30 s
  between liveness checks and usage-limit-park detections. Exits 143 on TERM
  (operator stop); exits 1 on unexpected tmux-session end (systemd restarts).
  When `TREADMILL_HOST` is set, runs a host-binding drift-guard preflight
  (ADR-0095) after the PID singleton check: curls the API for the label's bound
  host and exits 1 if bound elsewhere or unbound (fail-closed). See
  `../host_guard.py`.
- `treadmill-channel-reap` — `ExecStopPost` script. Tears down the label's tmux
  session; TERM→KILLs the PID-file lock-holder after verifying both cmdline
  identity and `TREADMILL_SESSION_LABEL` environ (PID-reuse guard); always
  exits 0. Also runs after failed starts, so a wedged crashloop self-heals.
- `treadmill-limit-park-check` — detects the Claude usage-limit modal. Reads
  the bottom 15 pane lines for the `limit to reset` signature AND requires two
  consecutive identical hashes (freeze guard against busy workers discussing
  the signature). Exit 0 = park confirmed; exit 1 = not parked.
- `treadmill-limit-park-recover` — recovery on a confirmed park. Arms an
  account failover (swaps `$STATE_ROOT/claude-account` → fallback, exits 0 →
  caller bounces) or escalates with a rate-limited relay+event (exits 2). HARD
  CONSTRAINT: never invokes `tmux` itself; the only key the platform sends at
  the modal is Enter (startup poller in the launcher).
- `treadmill-limit-park-sweep` — durable non-LLM fleet sweep (task 2b8fd900).
  Loops over all active `treadmill-channel@*` units, runs `park-check` on each,
  and bounces any parked unit via `systemctl --user restart`. Intended for the
  case where the orchestrator session itself is parked (its LLM is frozen, so
  it cannot create timers or relays). Always exits 0; runs as a systemd oneshot
  every 5 h (see `treadmill-limit-park-sweep.timer`).
- `treadmill-limit-park-sweep.service` — oneshot service wrapping the sweep.
- `treadmill-limit-park-sweep.timer` — `OnCalendar=*-*-* 00/5:00:00`,
  `Persistent=true` so missed windows fire on resume.
- `treadmill-team-control` — `<activate|pause> <repo-slug>`. Starts or stops all
  team units from `~/.treadmill/teams/<slug>/`. Pause is FAIL-CLOSED: refused
  unless the decision API affirmatively lists the team in `quiescent_teams`.
- `treadmill-team-scheduler.service` — runs the team-scheduler daemon (ADR-0091).
  Uses a host-global flock so a competing manual run exits 0 rather than
  loop-fighting.
- `treadmill-channel@coordinator-ramjac.service` — static override for the
  ramjac coordinator unit (non-template instance).

## Recent changes

> **New entries are PER-PR FRAGMENT FILES, not prepends** (task
> 986c5cf6): add `agent-changes/YYYY-MM-DD-<task-or-pr-slug>.md` beside
> this AGENT.md — one entry per file, newest by filename; format in
> `docs/agent-md-schema.md`. Prepending here is the conflict factory
> that stacked three same-day rework cascades on 2026-06-12 (every
> in-flight PR inserts at this same anchor).

## Pitfalls

- **Installed units are plain copies.** `~/.config/systemd/user/*.service` and
  `*.timer` are NOT symlinks; editing the repo originals has no effect until
  you `cp` + `systemctl --user daemon-reload`. Add this operator step to every
  PR that changes a unit file.
- **`treadmill-channel-launch` exits 143 on TERM** (intentional stop) and 1 on
  unexpected tmux-session end. Systemd sees 143 as `SuccessExitStatus` — no
  restart. The unexpected-exit path (1) triggers `Restart=on-failure`.
- **The launcher tree lives in the tmux server's cgroup**, not the unit cgroup.
  `KillMode=control-group` cannot reach it; `ExecStopPost` (`treadmill-channel-reap`)
  is the only reliable teardown path.
- **PID-reuse guard in `treadmill-channel-reap`**: the reap requires BOTH a
  matching cmdline AND `TREADMILL_SESSION_LABEL` in `/proc/<pid>/environ` before
  killing. A recycled PID on a sibling session is left alone.
- **Usage-limit-park detector reads BOTTOM 15 pane lines only** (bottom-anchor
  per PR #328 review note). Dismissed-modal residue scrolled into the upper
  region of a frozen idle pane must not re-trigger; the freeze requirement is
  the false-positive guard for busy workers whose transcript mentions the
  signature.
- **`treadmill-limit-park-recover` never invokes tmux.** Enter (the safe
  default / dismiss) is sent exclusively by the launcher's startup poller on
  relaunch. The billing options are operator-only and NEVER auto-selected.
- **Host-guard preflight is `TREADMILL_HOST`-gated.** When `TREADMILL_HOST` is
  unset the guard is inert — single-host setups without API bindings are
  unaffected. When set, the guard curls `TREADMILL_API_URL` (default
  `http://localhost:8088`) for the label's binding. If the API is unreachable or
  returns a non-OK response, curl exits non-zero, `_BOUND_HOST` stays empty, and
  the guard refuses start (fail-closed). `TREADMILL_HOST` is trusted without
  cryptographic verification; host-proof auth is ADR-0097 (out of scope).
- **`treadmill-limit-park-sweep` is a once-every-5h oneshot** — it relies on
  the `limit-park.state` file written by the launcher's own 30-s loop to
  confirm a park on a single call (no two-run delay). If the launcher's loop
  is also frozen (impossible under the current tmux-separate-process design,
  but defensive), the sweep only confirms on its second run (10 h delay).

## Navigation

- **Parent:** `tools/cc-channels/` — launcher, relay, access manager.
- **Tests:** `tools/cc-channels/tests/` — `test_limit_park.py` (park
  detection + recovery), `test_limit_park_sweep.py` (sweep), `test_channel_reap.py`,
  `test_launcher_singleton.py`, `test_team_control.py`, `test_host_guard.py`
  (host-binding drift guard).
- **Decisions:** ADR-0073 (persistent sessions), ADR-0091 (team scheduler +
  pause fail-safe).
