---
date: 2026-09-14
trigger: surprise
status: captured
related: ADR-0101, ADR-0113
---

# Learning: a NEW team's sessions crash-loop on claude's folder-trust prompt

## Trigger
Bringing up the first team on a fresh repo (`netlify/agent-runner-orchestrator`, the
ADR-0113 dogfood), all 5 sessions (coordinator + evaluator + 3 workers) crash-looped —
`treadmill-channel-launch: tmux session ... ended unexpectedly` every ~30s, systemd
restart counter climbing. A controlled re-run of the launcher captured the cause: claude
was sitting at the workspace folder-trust prompt and exiting.

## Observation
`launch-session.sh`/`treadmill-channel-launch` auto-dismisses two startup prompts by
scraping the pane. For the trust prompt (`Quick safety check: Is this a project you
trust?` → `❯ No, exit` / `Yes, I trust this folder`) it sends `"1" Enter`. But this
claude version's trust prompt is ARROW-SELECT with no "1" hotkey — so "1" is a no-op and
the trailing Enter selects the DEFAULT, `❯ No, exit`, and claude exits. The dev-channels
prompt handler right beside it deliberately uses `Down Enter` (not a hotkey) for exactly
this reason; the trust handler was not updated to match. Existing teams never hit it
because their session dirs were already `hasTrustDialogAccepted: true` in `~/.claude.json`
from an earlier interactive accept — so this is LATENT: it only bites a brand-new team's
never-before-opened session dirs.

## Generalization
A pane-scraping auto-answer is coupled to the exact prompt LAYOUT of a specific claude
version; a claude update that changes a prompt from numbered to arrow-select silently
breaks it, and the failure is invisible until the next NEW session dir is opened. Any
supervised-launch prompt handler that sends a hotkey (rather than `Down…/Enter` relative
navigation) is fragile across claude versions.

## Proposed rule
`treadmill team up` must pre-trust every session dir it creates — set
`hasTrustDialogAccepted: true` for each `~/.treadmill/teams/<slug>/<label>` in
`~/.claude.json` at creation — so a new team never depends on scraping the trust prompt
at launch. Independently, the launcher's trust-prompt handler should navigate with
`Down Enter` (select the non-default "Yes, I trust this folder"), never the "1" hotkey,
matching the dev-channels handler.

## Proposed remediation
Two-layer (defense-in-depth): (1) pre-trust in `treadmill team up` (deterministic, no
timing/scrape dependency — the primary fix); (2) fix the launcher trust handler to
`Down Enter` (backstop for any dir not pre-trusted). For the netlify dogfood I applied
the pre-trust manually to the 5 session dirs; sessions then stayed up (NRestarts stable).

## Notes
Same class as the exec-in-charge skill's "root fix to file: the launch config should
auto-answer the startup channel-approval prompt so the team never hangs on a reboot" —
this is the trust-prompt sibling of that hang, but it CRASH-LOOPS (exits on the default)
rather than hanging. Caught during the ADR-0113 collaborator-repo dogfood.
