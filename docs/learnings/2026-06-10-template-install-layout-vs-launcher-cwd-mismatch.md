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
sessions to pick up the new templates." Inspecting the live `coordinator-ramjac`
on disk before restarting revealed the restart would have been a no-op — the running
session reads a different file than `install_team()` writes.

## Observation
Three concrete layout mismatches between `tools/team-templates/install.py` and
`tools/cc-channels/launch-session.sh`:

1. `launch-session.sh` pins a coordinator's cwd to the team dir
   `~/.treadmill/teams/<slug>/` and sources `<team>/coordinator.env`. Claude Code
   reads `CLAUDE.md` from cwd (and parents). `install_team()` renders the coordinator
   prompt to the per-label *subdir* `<team>/coordinator-<slug>/CLAUDE.md`, which the
   running session never reads. The live coordinator-ramjac was still reading the
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

The compound effect: a restart of `coordinator-ramjac` would re-load the stale
ADR-0084 prompt from the root file (winning hierarchically over the un-read
per-label new file) and worker spawns would have no relay-inject hook registered.

## Root cause
Three separate authors over three separate PRs each made a locally reasonable
choice that did not compose:

- PR-B (Bert, `treadmill team up`): created per-label subdirs `<team>/<label>/`
  for the `.session-id` stub + per-label `.env` file. Operator-readable. Did not
  move other artefacts.
- PR-E (Carla, `tools/team-templates/install.py`): rendered per-session CLAUDE.md
  and settings.json into the per-label subdirs PR-B created. Followed the
  operator-readable layout from PR-B. Wrote `<label>/settings.json` because that's
  the per-label dir, not knowing Claude Code reads `.claude/settings.json`
  specifically.
- PR-D (Bert, coordinator template + install extension): added
  `coordinator/CLAUDE.md.tmpl` and extended `install.py` to render the
  coordinator's CLAUDE.md into `<label>/`. Inherited the same layout assumption.

The launcher (`launch-session.sh`) predates ADR-0087 entirely. Its coordinator-cwd
branch (ADR-0084 §3A) was correct for the v1 coordinator model that put the prompt
at `<team>/CLAUDE.md`. No PR touched the launcher because nobody noticed the layout
mismatch — the rendered files existed, the launcher started a session in a related
directory, the session ran. The disconnect was silent at runtime: Claude Code
happily auto-discovered the OLD CLAUDE.md and ran without complaint.

Each PR ran its own unit tests. None ran the end-to-end "render + launch + verify
the rendered files are read" check, because that needs a Claude Code process AND a
session restart AND a disk-state inspection — outside the usual unit-test loop. The
first integration verification on disk was the pre-restart audit. That is the test
that caught it.

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

## Remediation (shipped as PR-H, #293)
A single wiring PR reconciles all three seams:

1. **`tools/cc-channels/launch-session.sh`**: extends the per-label workdir
   handling from coordinator-only to coordinator + evaluator + worker. All three
   roles `cd` to `~/.treadmill/teams/<slug>/<label>/` so Claude Code's
   auto-discovery reads the rendered files. The coordinator continues to source
   `<team>/coordinator.env` from the team root BEFORE the cwd change (the env
   file's path predates per-label dirs and the API's plan-id write-through
   depends on it). All three roles also source the per-label `<label>/<label>.env`
   written by `treadmill team up`.
2. **`tools/team-templates/install.py`**: worker `settings.json` is rendered to
   `<label>/.claude/settings.json` (creating `.claude/`). `install_team()` unlinks
   the stale `<team>/CLAUDE.md` if present on every run — idempotent on the
   second run.
3. **Regression tests** (`tools/team-templates/tests/test_install.py` +
   `tools/cc-channels/tests/test_coordinator_launch.py`): pin the
   `.claude/settings.json` path, the stale-cleanup behaviour, and the per-label
   cwd for every role family so a future refactor cannot reintroduce either bug.

### Transcript migration
Only `coordinator-ramjac` had a live transcript before PR-H. Its cwd changes
from `<team>/` to `<team>/coordinator-ramjac/`, which changes the
`claude --resume` transcript directory slug (see
`2026-06-04-systemd-default-cwd-breaks-claude-resume.md`). The chosen migration is
**option 3** (Bert + Alan + Carla consensus): accept the transcript loss; rely on
the new template's §2 startup recovery (stale-row sweep + own-inbox drain +
mergeability re-poll + events-table `plan.submitted` replay) to rebuild state.
`memory.md` persists separately and is reloaded from cwd on first turn. No
transcripts exist for any worker or evaluator session — they're brand new.

## Notes
Surfaced during the ADR-0087 North Star push (get a plan driven to merge by the team
with no Alan/Bert/Carla intervention). This is the gating work between "templates
merged" and "team boots and self-drives." Relayed to Bert (owns launcher/CLI session
wiring, PR-B/PR-D) and Carla (owns install.py, PR-E).

Two further lessons beyond the proposed rule:

1. **Integration tests cost what they save.** Three correct contributors over three
   PRs reproduced this gap precisely BECAUSE each ran their own unit tests and
   trusted the others'. The on-disk verification step is the cheap version of an
   integration test; it should be a planned step in any subprocess-spawn-driven
   track, not a pre-restart sanity check.
2. **Launchers are shared infrastructure.** The launcher's coordinator-cwd handling
   predates this track by 3+ ADRs and was correct for the old model. It became
   wrong silently when the model changed. Any new role family added to the team
   needs the launcher's per-label handling extended — PR-H makes that coupling
   visible in the launcher comments.
