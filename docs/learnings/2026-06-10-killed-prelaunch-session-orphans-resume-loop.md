---
date: 2026-06-10
trigger: surprise
status: captured
related: ADR-0087, ADR-0073
---

# Learning: killing a wedged claude before its transcript exists leaves a session-id stub that --resume can't satisfy

## Trigger
During the ADR-0087 ramjac team boot (2026-06-10), both worker sessions wedged on a
settings-validation prompt. We killed the tmux sessions and restarted the systemd units.
The units then crash-looped 25+ times in ~12 minutes: `tmux session ended unexpectedly`
every ~30 seconds, empty panes, no visible error.

## Observation
The launcher (`launch-session.sh`) mints a session id on first boot, records it in
`~/.cc-channels/<label>/session-id`, and launches with `--session-id <sid>`. On every
subsequent boot the file exists, so the launcher uses `--resume <sid>`. But the first
claude was killed at the settings prompt — before it ever wrote a transcript. `--resume`
against a session id with no transcript fails immediately, the pane dies, systemd
restarts per `Restart=on-failure`, and the loop never self-heals because the session-id
file persists across restarts.

## Generalization
The session-id file is written optimistically at launch, but it only becomes a valid
resume target after claude persists a transcript. Any kill in the window between launch
and first transcript write (wedge prompts, fast crashes, OOM at boot) leaves a poisoned
stub that turns `Restart=on-failure` into an infinite crashloop. The wrapper's
restart-counter grows silently; nothing escalates.

## Proposed rule
When manually killing a team/channel session that never reached a working prompt, also
remove `~/.cc-channels/<label>/session-id` (and `launcher.pid`) before restarting the
unit. Structurally: the launcher should validate that a transcript exists for the
recorded session id before choosing `--resume`, and fall back to minting a fresh id
(logging the orphan) when it doesn't.

## Proposed remediation
Launcher fix: before `SESSION_ARGS=(--resume "$sid")`, check
`~/.claude/projects/<cwd-slug>/<sid>.jsonl` exists and is non-empty; if not, mint a
fresh id and overwrite the file. Bounded fix, removes the whole footgun class.
Candidate small PR after the ADR-0087 waves land.

## Notes
Recovery: stop unit → `tmux kill-session` → `rm session-id launcher.pid` →
`reset-failed` → start. Both workers booted clean immediately after.
