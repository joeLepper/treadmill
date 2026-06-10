---
date: 2026-06-10
trigger: surprise
status: captured
related: ADR-0087, plan-2026-06-10-adr-0087-implementation
---

# Learning: install_team() output layout doesn't line up with how the launcher + Claude Code consume CLAUDE.md / settings.json

## Trigger
After merging ADR-0087 PR-D (#292, coordinator template) and PR-E (#291, worker
hooks + install.py), the plan's next step was "restart the live coordinator/worker
sessions to pick up the new templates." Inspecting the live `coordinator-medicoder`
on disk before restarting revealed the restart would have been a no-op — the running
session reads a different file than `install_team()` writes.

## Observation
Three concrete layout mismatches between `tools/team-templates/install.py` and
`tools/cc-channels/launch-session.sh`:

1. `launch-session.sh` pins a coordinator's cwd to the team dir
   `~/.treadmill/teams/<slug>/` and sources `<team>/coordinator.env`. Claude Code
   reads `CLAUDE.md` from cwd (and parents). `install_team()` renders the coordinator
   prompt to the per-label *subdir* `<team>/coordinator-<slug>/CLAUDE.md`, which the
   running session never reads. The live coordinator-medicoder was still reading the
   stale ADR-0084 `<team>/CLAUDE.md` (23KB, pre-ADR-0087).
2. Worker `settings.json` is rendered to `<team>/<label>/settings.json`, but Claude
   Code discovers project settings at `<cwd>/.claude/settings.json`. The PostToolUse
   relay-inject hook is therefore never registered for a worker session.
3. The launcher has special workdir handling ONLY for `coordinator-*` labels.
   Evaluator + worker sessions run from the default workdir ($HOME/treadmill), nowhere
   near their rendered per-label files.

Compounding (1): Claude Code loads CLAUDE.md hierarchically (cwd + every parent), so
even moving a session's cwd into its per-label subdir would still inherit the stale
`<team>/CLAUDE.md` as a parent. The reconciliation has to remove/replace that root
file too.

## Generalization
When two halves of a feature are built in parallel by different sessions against a
prose spec ("install templates into the session tree" / "launch sessions from the
team dir"), each half can be internally correct and fully tested yet not compose,
because the spec never pinned the exact file paths Claude Code consumes (cwd/CLAUDE.md,
cwd/.claude/settings.json). Structural tests that assert "the rendered file exists at
path X" pass while the runtime reads path Y. The integration only fails at boot, which
no unit test in either half exercises.

## Proposed rule
For any feature that renders config a runtime later loads by convention (CLAUDE.md,
.claude/settings.json, .env), the plan task MUST pin the exact consumer path, and at
least one test must assert the file lands where the consumer actually reads it — not
merely where the writer chose to put it.

## Proposed remediation
A wiring PR that reconciles install_team() output with launcher + Claude Code
discovery. Leading option: launcher sets each session's cwd to its per-label subdir
`<team>/<label>/`; install_team() writes `<label>/CLAUDE.md` + `<label>/.claude/settings.json`;
the stale `<team>/CLAUDE.md` is removed; coordinator.env is read from the team dir
regardless of cwd. HAZARD: changing the coordinator's cwd changes the
`claude --resume` transcript slug (per
`2026-06-04-systemd-default-cwd-breaks-claude-resume.md`), which would orphan the live
coordinator-medicoder transcript — needs a migration step. Resolve via sibling review
before implementing.

## Notes
Surfaced during the ADR-0087 North Star push (get a plan driven to merge by the team
with no Alan/Bert/Carla intervention). This is the gating work between "templates
merged" and "team boots and self-drives." Relayed to Bert (owns launcher/CLI session
wiring, PR-B/PR-D) and Carla (owns install.py, PR-E).
