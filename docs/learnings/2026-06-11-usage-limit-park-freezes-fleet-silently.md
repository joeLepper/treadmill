---
date: 2026-06-11
trigger: surprise
status: crystallized-into-rule-limit-park-detected-and-recovered
crystallized_into: tools/cc-channels/systemd/treadmill-limit-park-check
related: ADR-0055 (per-account creds), ADR-0066 (usage-limit fallback), ADR-0087 (team execution)
---

# Learning: a Claude usage-limit park freezes a worker silently; auto-fallback can't catch it

## Trigger
2026-06-11: the ramjac fleet's dispatched work froze for ~3h. Tasks
showed "worker-X: executing" with zero branches/commits. Donna and the
coordinator escalated it as the orphan-crashloop class; the operator-side
tmux look found the real cause — workers parked at the interactive Claude
usage-limit prompt ("Stop and wait for reset / Switch to usage credits /
Switch to Team plan"). The worker's account had hit its weekly cap.

## Observation
The CLI surfaces a weekly-limit as an INTERACTIVE MODAL, not a non-zero
exit. So:
- The worker session sits at the prompt, consuming its claim, producing
  nothing, and emitting no exit/blocked relay — invisible to the
  coordinator (which can't see tmux).
- ADR-0066's usage-limit fallback never fires: it triggers on a non-zero
  exit + usage-limit signature, and there is no exit — the process blocks
  on stdin. (Joe predicted this: "I'm skeptical that can work.")
- A plain unit restart does NOT clear it: the resumed session redraws the
  stale modal from its transcript.

## Generalization
Any interactive-blocking CLI state (limit prompt, an unexpected confirm,
an auth re-prompt) defeats exit-code-based health + fallback and presents
as a silent worker stall. Detection must read the SESSION SURFACE (the
pane), not just the exit code; and the orchestration layer is blind to
tmux, so a human/operator look was the only diagnosis path today.

## Manual recovery (until automated — the recipe)
1. Confirm the park: `tmux capture-pane -t <label> -p | grep "limit to reset"`.
2. Ensure the good token is the active account in `~/.claude` (or the
   session's CLAUDE_CONFIG_DIR) — workers and orchestrators share the
   default `~/.claude` today.
3. Bounce the unit: `systemctl --user restart treadmill-channel@<label>`
   (reloads the credential).
4. Dismiss the stale modal: `tmux send-keys -t <label> Enter` — THIS is
   the step a bounce alone misses; without it the resumed session keeps
   showing the limit screen.
5. The coordinator re-engages (or the resumed session pre-drains a pending
   ping) and drives its claimed exec; no reclaim needed.

## Proposed rule
A worker parked at an interactive prompt is a stall the platform must
detect and auto-recover (or escalate with the recipe) within minutes, not
hours — never a silent freeze.

## Proposed remediation
Registered task (treadmill team): pane-signature detection of the
limit-park + auto-failover to a configured non-limited account
(CLAUDE_CONFIG_DIR swap + bounce + auto-Enter), or escalate to the owning
coordinator/orchestrator with the exact recipe when no fallback is
configured. Must NEVER auto-select the billing options (usage credits /
Team plan cost money — operator only).

## Notes
Secondary: the ADR-0055/0066 per-account system (CLAUDE_CONFIG_DIR +
~/.claude-<account> dirs) is the swap substrate, but it is NOT wired into
the tmux launcher today — every session uses default ~/.claude. Making
account selection first-class per session is a prerequisite for clean
auto-failover.
