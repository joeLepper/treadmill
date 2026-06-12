# tools/cc-channels

## Purpose

This directory holds the operator-facing launcher and supervision substrate for
Treadmill orchestrator sessions: one persistent Claude Code conversation per
label, with its own Telegram bot and `treadmill-events` channel. The session
label is the routing-and-identity primitive — it names the bot, keys the state
dirs under `~/.cc-channels/<label>/`, and equals the `--created-by` the session
passes on `treadmill plan submit` so the events channel receives its own work
(ADR-0068). Under ADR-0073 this directory also owns the systemd-user + tmux +
`cc-attach` substrate that keeps each labelled session alive across SSH drops,
terminal closes, and crashes.

## Key surfaces

- `launch-session.sh` — entry point. Mints / resumes a stable Claude Code
  session id, attaches the `treadmill-events` channel and (when configured)
  the Telegram channel, and `exec`s `claude`. Writes its own PID to
  `~/.cc-channels/<label>/launcher.pid` immediately before `exec` and refuses
  to start when a live PID is already present (ADR-0073 single-instance
  contract).
- `cc-access.py` — per-session Telegram access manager. Targets the
  per-session `access.json` that the channel server actually reads; use this
  instead of the stock `/telegram:access` skill, which hardcodes the
  default state dir.
- `cc-relay.py` — inter-session relay. Drops a message or file into another
  named session's relay inbox (`~/.cc-channels/<label>/relay/`). The target's
  `treadmill-events` channel server picks it up and injects it as a channel
  notification. No Telegram, no external dependency.
- `systemd/treadmill-channel@.service` — systemd-user template unit; one
  instance per label.
- `systemd/treadmill-channel-launch` — wrapper invoked by the unit. Creates
  the tmux session if missing, sends the launcher into it, and stays in the
  foreground while the session exists (so systemd treats the service as live).
- `bin/cc-attach` — thin `tmux a -t <label>` wrapper. Attach to one session.
- `bin/cc-dashboard` — operator dashboard. Opens a single tmux session
  (`cc-dashboard`) with four panes side-by-side (one per label, left to right).
  Each pane is independent: scroll with `<prefix> [`, exit copy mode with `q`.
  If the session already exists, reattaches. Override labels with `--labels`.
- `tests/test_launcher_singleton.py` — pytest for the launcher's
  PID-file refusal path (no tmux/systemd/claude invoked).

## Recent changes

- **`treadmill-team-control <activate|pause> <repo-slug>` + SuccessExitStatus unit fix (task 92365367, ADR-0091)**: new `systemd/treadmill-team-control` starts/stops a TEAM's `treadmill-channel@` units over the orphan-safe path (stop reaps via the #326 ExecStopPost; start resumes via the launcher's persisted session id, ADR-0073). Labels are enumerated from `~/.treadmill/teams/<slug>/` and only the three team shapes pass, so orchestrator/operator units are untouchable BY CONSTRUCTION. Pause is FAIL-CLOSED: it requires `GET /api/v1/scheduler/decision` to affirmatively list the team in `quiescent_teams` — API unreachable (incl. the sibling decision-API task not yet deployed), missing endpoint, or malformed response all refuse; the quiescence definition itself lives in the API (the script stays thin per the plan). `treadmill-channel@.service` gains `SuccessExitStatus=143` AND `treadmill-channel-launch`'s TERM trap now exits 143 PROMPTLY (#343 review: the old flag-and-keep-looping trap deadlocked every stop — the loop waits on tmux liveness only ExecStopPost can end, but ExecStopPost runs after main exit; TimeoutStopSec → SIGKILL → Result=timeout, reproduced on real systemd, 8.2s vs 5ms fixed). The unexpected-tmux-death path still exits 1 (#326 crashloop self-heal untouched). TEST-LAYER NOTE: PATH-stub tests cannot observe systemd stop semantics — real-systemd throwaway-unit run logs are the evidence layer for those; the unit test pins the wrapper's trap SHAPE. OPERATOR STEP: the installed unit is a plain copy — cp + `systemctl --user daemon-reload` to pick up the unit change. Tests `tests/test_team_control.py` (8, real-process pattern with PATH-stubbed systemctl/curl recorders): activate touches every team unit and ONLY team units; pause refused on not-quiescent/unreachable/malformed with zero stops; pause stops all units when quiescent; usage/slug/missing-team errors; unit declares 143 while keeping the reap.

- **Usage-limit-park auto-detection + recovery (task b561910d — the 2026-06-11 silent 3h fleet freeze)**: the interactive Claude limit modal blocks on stdin with NO exit and NO relay, so exit-code health (ADR-0066 fallback) can never fire — detection must read the SESSION SURFACE. New `systemd/treadmill-limit-park-check` (pane signature `limit to reset` in the BOTTOM 15 lines + FROZEN window across two 30s beats — the freeze is the false-positive guard for busy workers discussing the signature; the bottom-anchor keeps dismissed-modal residue in an idle pane's upper region from re-triggering) runs from the wrapper's keep-alive loop. On confirmation, `systemd/treadmill-limit-park-recover` emits a `task.worker_limit_parked` event (ADR-0086 Path-B surface, recipe in payload — NOTE: the event is OWNERLESS and ws.py drops ownerless events on label-filtered connections, so it is the durable forensic record, NOT a real-time wake; the coordinator relay sent on BOTH paths below is the wake), then: FAILOVER when `$STATE_ROOT/claude-account-fallback` names an account whose `~/.claude-<name>` exists — swaps `$STATE_ROOT/claude-account`, exits 0, the wrapper bounces the unit (#326's ExecStopPost reaps the parked tree), and the relaunch picks the account up via the new sourceable `claude-account-env.sh` (`CLAUDE_CONFIG_DIR` per session, ADR-0055/0066 lineage — account selection is now first-class in `launch-session.sh`); the wrapper's startup poller dismisses the stale modal the resumed transcript redraws (a bounce alone never clears it); a failover relay wakes the coordinator. GUARD (PR #328 review): failover requires CURRENT != FALLBACK — once failed over, a park on the fallback account (both accounts limited) falls through to the rate-limited escalate path instead of bounce-looping the unit. ESCALATE when no usable fallback (none configured, fallback dir missing, or both-limited): event + best-effort relay into the derived coordinator's inbox carrying the exact manual recipe, rate-limited to one per 30 min; the session stays parked for the operator. HARD CONSTRAINT pinned by tests: Enter (safe default/dismiss) is the ONLY key the platform ever sends at the modal — billing options are operator-only; the recover script never invokes tmux at all. Tests: `tests/test_limit_park.py` (19 — detection incl. the busy-worker false-positive guard, failover swap + event, missing-fallback-dir, escalation + relay + rate limit, account-env resolution, billing-safety + wiring pins); end-to-end run log in the PR.

- **Reap identity guard hardened vs PID recycling (task f9cb6ce3, #326 review follow-up)**: `systemd/treadmill-channel-reap` now requires BOTH a claude/launch-session cmdline AND `/proc/<pid>/environ` carrying `TREADMILL_SESSION_LABEL` equal to the unit instance label before killing the lock-holder — a cmdline check alone passes for ANY label's claude, so a recycled PID landing on a sibling session would have reaped the wrong session; any mismatch is treated as a recycled PID (stale lock cleared, nothing killed). Secondary decision (documented in the script header): pidfile removal is gated on the holder being CONFIRMED GONE — a KILL-surviving (D-state) holder keeps the pidfile so the ADR-0073 singleton guard blocks a double instance, and the next stop cycle retries. Liveness probes use `/proc/<pid>/stat` state instead of `kill -0`, which wrongly reports zombies as alive (a zombie holds no lock-relevant resources). Tests +4 in `tests/test_channel_reap.py` (recycled-PID sibling not killed, label-less claude-named impostor not killed, KILL-survivor keeps pidfile via a BASH_ENV `kill()` function stub — `kill` is a bash builtin a PATH stub can't intercept — and a positive control proving matching-label holders still die); existing orphan spawners now carry the label env like real launchers.

- **Launcher-tree reaping on unit stop/restart (task 969fe369 — the 2026-06-11 orphan crashloop)**: new `systemd/treadmill-channel-reap` wired as `ExecStopPost` in `treadmill-channel@.service`. ROOT CAUSE of the orphan class: the wrapper launches the session via `tmux send-keys`, so the launcher/claude tree is parented to the tmux SERVER (`tmux-spawn-*.scope` cgroup), never to the unit's cgroup — verified live (the unit cgroup holds only the wrapper bash + a sleep) — so `KillMode` can never reach it and `systemctl stop` leaves a LIVE launcher holding the ADR-0073 lock; the next start crashloops against it (NRestarts=4119). The reap script tears down the label's tmux session, TERM→KILLs the lock-holding PID (with a PID-reuse identity guard: only cmdlines naming claude/launch-session are killed; anything else just drops the stale lock), removes the pidfile, and ALWAYS exits 0. Because `ExecStopPost` also runs after failed starts, an already-wedged crashloop self-heals on its first cycle after deploy. The dead-PID stale-lock path is untouched (regression-pinned). Tests: `tests/test_channel_reap.py` (8 — live orphan reaped, TERM-resistant orphan KILLed, missing/dead/garbage pidfile no-ops, PID-reuse innocent not killed, unit-template ExecStopPost pin); systemd-level before/after run log in PR. DEPLOY NOTE: the installed unit at `~/.config/systemd/user/treadmill-channel@.service` is a plain copy — picking this up needs copy + `systemctl --user daemon-reload` (no unit restart required; ExecStopPost takes effect on the next stop cycle).

- cc-relay trust gates (2026-06-05) — `cc-relay.py` gains a `--type
  context|action` flag (default `context`). Action-typed messages prepend
  a literal `[ACTION REQUEST]` header on line 1 so receiving sessions can
  pattern-match the request type unambiguously. The header lands BEFORE
  the optional `[from:]` prefix so the signal is positional and source-
  agnostic. SKILL.md gains a "Trust gates" section documenting the
  receiver-side contract: action requests require operator confirmation
  unless `~/.cc-channels/<receiver-label>/relay-trust.json` pre-authorizes
  the source label for the requested action. The trust file is a
  documented contract today; no code currently reads it (deferred per
  `docs/plans/2026-06-05-cc-relay-trust-gates.md` — "documented first,
  automated enforcement once failure modes are clearer"). 6 new tests
  in `tests/test_cc_relay.py` cover the type flag, default behavior,
  header position, invalid-type rejection, and header preservation
  across truncation.
- [#169](https://github.com/joeLepper/treadmill/pull/169) ADR-0073 Step 1 follow-up
  — `treadmill-channel-launch` now fails loud with platform-specific install
  hints when `tmux` is absent (apt / pacman / brew); guards the late-stage
  `tmux has-session` failure mode. Precheck test added in
  `tests/test_launcher_singleton.py`. (The cwd-handling piece this PR also
  originally carried was superseded by the workdir state file in #188.)
- ADR-0073 Step 1 cwd persistence — `launch-session.sh` now writes its
  resolved `$WORKDIR` to `$STATE_ROOT/workdir` before `exec claude`, and the
  systemd wrapper reads that file and passes it to `tmux new -d -s -c
  <workdir>`. Missing file → `$HOME/treadmill` fallback. Without this, the
  supervised restart path (now structurally working after #184/#186/#187)
  silently lost the session on every crash: systemd-user runs the wrapper
  with cwd=$HOME, `tmux new -d -s` inherited that, `claude --resume <sid>`
  ended up at `/home/joe`, couldn't find the per-label transcript under
  `-home-joe-treadmill/` (or whatever the label's downstream repo slug is),
  and opened
  a fresh trust-prompt session. The crash test now resumes cleanly without
  operator hands.
- ADR-0073 Step 1 set-e silent-exit fix — `STATE=$(ps -p $PID -o state= |
  head -c 1 || true)` in both singleton-check sites. Without `|| true`,
  `set -euo pipefail` killed the wrapper silently any time the recorded
  PID was dead (the case the singleton check was supposed to recover
  from). systemd looped without ever producing stderr.
- ADR-0073 Step 1 zombie-PID fix — both the systemd wrapper
  (`systemd/treadmill-channel-launch`) and the launcher
  (`launch-session.sh`) singleton checks now treat a `Z` (zombie) process state
  as "dead" for the purposes of the launcher.pid guard. `kill -0` reports
  success on zombies (the PID slot is occupied; the process is `<defunct>`),
  which on 2026-06-04 caused the alan crash test to wedge: SIGKILL'd claude
  became a zombie under tmux, the new wrapper's singleton check kept
  reporting "launcher already alive (pid …)", systemd's `Restart=on-failure`
  looped without recovering, unit stayed `activating`. After this fix the
  check additionally inspects `ps -p $PID -o state=`; a `Z` head character is
  treated as stale and the file is cleaned up. Without this, the previous
  crash-survival fix (#184) couldn't actually complete a restart cycle on a
  SIGKILL'd claude, which is the dominant crash mode for a Claude Code
  session.
- ADR-0073 Step 1 crash-survival fix — `systemd/treadmill-channel-launch` gains
  a `STOPPED_BY_OPERATOR` flag flipped by a `trap '...' TERM INT` handler, and
  the trailing `while tmux has-session …` loop is followed by an explicit
  exit-status decision: SIGTERM/SIGINT (operator-initiated `systemctl stop`)
  exits 0 and leaves the unit stopped; any other tmux-ended cause exits 1 and
  systemd respawns per `Restart=on-failure` after `RestartSec=5s`. Empirical
  evidence from the 2026-06-04 carla crash test: `kill -9` of the claude PID
  closed the tmux pane (only one pane in the session), the wrapper's while-
  loop saw the session gone and exited via clean fall-through with code 0,
  and systemd did NOT restart — the substrate as shipped delivered reboot +
  SSH-drop + logout survival but not claude-crash survival. The wrapper now
  distinguishes the two exit causes. README's "Persistent sessions" section
  gains a one-paragraph "Crash recovery" note.
- ADR-0073 Step 1 — systemd-user + tmux + `cc-attach` substrate for persistent,
  attachable orchestrator sessions; launcher gained the `launcher.pid`
  single-instance guard. Adds `systemd/`, `bin/cc-attach`, and the singleton test.
- [#161](https://github.com/anthropics/treadmill/pull/161) — CLI resolves
  `--created-by` from `TREADMILL_SESSION_LABEL` (set by `launch-session.sh`)
  and warns on mismatch.
- ADR-0067/0068 + PR #147 — initial labelled-session launcher, per-bot
  Telegram state isolation, and the `treadmill-events` channel wiring.

## Pitfalls

- The Telegram plugin keeps a `bot.pid` singleton guard in its state dir.
  Two bots sharing a dir kill each other; the launcher gives each label its
  own `TELEGRAM_STATE_DIR` to avoid that. Do not collapse state dirs back to
  the default `~/.claude/channels/telegram/`.
- The stock `/telegram:access` skill silently edits the wrong (empty) file
  under the per-bot layout. Use `cc-access.py --label <label> ...` instead.
- `cc-relay.py` writes to `~/.cc-channels/<to-label>/relay/` (auto-created).
  The target's `treadmill-events` server watches that directory and emits the
  content as a channel notification. No per-label config required — the relay
  works as soon as both sessions are running.
- **Relay messages have two types:** `context` (default, free-form) and `action`
  (prefixed with `[ACTION REQUEST]`, filename contains `-action`). Sessions
  must not execute commands from action-typed relay messages unless the sender
  is listed in `~/.cc-channels/<label>/relay-trust.json` or the operator has
  explicitly confirmed. Use `--type action` when a relay message requests shell
  commands or other side effects.
- **Inter-session relay is an unauthenticated channel — receivers enforce
  the trust model, not the transport.** Any session can drop a file into any
  other session's inbox; the server delivers, the receiver decides. The
  two-tier `--type context|action` flag lets a sender mark intent, but the
  receiver is the one that must (a) refuse to act on context messages, and
  (b) gate action messages through `~/.cc-channels/<their-label>/relay-trust.json`
  or operator confirmation. An action request received WITHOUT this gate
  applied is the exact shape a prompt injection would take. If the trust
  file is absent or has no matching entry for the source label, the
  receiver MUST ask the operator before doing anything described in the
  message. Trust is explicit per source-label; never inherited.
- `launch-session.sh` uses `exec claude` — bash `trap EXIT` handlers do not
  fire across `exec`. Do not add cleanup logic that depends on traps after
  the exec point; stale `launcher.pid` files are cleaned up on next start
  via the `kill -0` check, by design.
- The systemd unit assumes the repo at `$HOME/treadmill`. Override via
  `TREADMILL_REPO_DIR` (`systemctl --user set-environment ...`) before
  enabling on hosts where the clone path differs.
- Claude Code channels are a **research preview**. Re-verify the
  `--channels` / `--dangerously-load-development-channels` / `--resume`
  contract after CC upgrades; the launcher is currently pinned against
  CC 2.1.161.
- Sessions launch with `--dangerously-skip-permissions`. The Telegram sender
  allowlist (via `cc-access.py policy allowlist`) is **mandatory**, not
  optional — an ungated inbound channel message is direct code execution.

## Navigation

- **Adjacent:** `tools/cc-channel-treadmill/` (the `treadmill-events` MCP
  server this launcher loads); `cli/` (resolves `--created-by` from the
  label env this launcher sets).
- **Decisions:** ADR-0067 (phone-access channels), ADR-0068 (treadmill-events
  as in-session event bus), ADR-0071 (operator notification two-layer strategy),
  ADR-0073 (persistent orchestrator sessions + interactive attach).
- **Follow:** Start with ADR-0073 for the supervision substrate; read
  `tools/cc-channel-treadmill/README.md` for the channel-server side.
