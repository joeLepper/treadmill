# Handoff — 2026-06-03: CC Channels for phone access — research done, setup not started

## Where we left off

Research session (in the osmo repo session, but the work is Treadmill-adjacent
agent-ops, hence docs here). Goal: interact with Joe's ~5 concurrent long-running
Claude Code sessions from his phone.

**Decision settled and recorded as ADR-0067** (proposed): Claude Code Channels,
one bot per session, Telegram by default. Remote Control rejected on Joe's direct
experience (timeouts, dead sessions, bad UX as an early adopter). See the ADR for
the full alternatives analysis.

Key research verdicts (3 claude-code-guide agent runs, docs + plugin READMEs):

| Question | Verdict |
|---|---|
| Can Channels drive a session from a phone? | Yes — chat-mediated (Telegram/Discord/iMessage), not a remote terminal |
| Multi-session on one bot? | No. Fan-out not demux; no session addressing in the protocol |
| Telegram single-bot sharing | Hard-broken: `getUpdates` allows one poller per token (`409`) |
| Discord single-bot + N channels | Stock plugin exposes no shard/channel→session routing; also one bot per session |
| Only per-session isolation knob | Separate bot token + `TELEGRAM_STATE_DIR`/`DISCORD_STATE_DIR` |
| Permission relay across sessions | Undefined in docs (`request_id` is session-local) — moot here, permissions bypassed |

Confidence note: the 409/single-poller constraint and per-session-process model are
solid; "Discord plugin has no shard knob" is inferred from plugin READMEs, not
doc-guaranteed. Worth a 2-minute re-verify against plugin source before building.

## Machine state at handoff

- Workstation: Claude Code **2.1.161** ✓ (need ≥ 2.1.80)
- **Bun: not installed** ✗ — the one missing prerequisite (plugins are Bun scripts)
- No channel plugins installed, no bots created, no launcher written
- ADR-0067 written, uncommitted, in this repo's working tree

## What needs Joe's hands

1. **Create the bots in BotFather** (one per session, ~5). Name them after the
   sessions (e.g. `osmo_forecast_bot`, `osmo_galaxy_bot`) so the Telegram chat list
   reads as the session list. Collect the 5 tokens.
2. **Approve Bun install** (system-wide runtime, `curl -fsSL https://bun.sh/install | bash`
   or distro package — orchestrator should not install without the nod).
3. **Confirm Telegram over Discord** — weakly-held default in ADR-0067; the
   one-bot-per-session shape is identical either way, so this is low-stakes.

## What I would do next session

In order:

1. Install Bun; verify `bun --version`.
2. `/plugin install telegram@claude-plugins-official`, then `/telegram:configure <token>`
   for the first bot.
3. Write `launch-session.sh <label> <token>` — sets a per-session
   `TELEGRAM_STATE_DIR` (e.g. `~/.cc-channels/<label>/`), configures the token, and
   launches `claude --channels plugin:telegram@claude-plugins-official` with the
   session's usual flags (bypassed permissions, cwd).
   Open question: where does the launcher live? A dedicated personal repo
   (`claude-ops`?) was floated and not settled — for now it can sit next to these docs
   or in `~/bin`.
4. Pair Joe's Telegram account against bot #1 (message the bot, approve pairing
   code in-session). **Configure the sender allowlist before anything else** —
   bypassed permissions + open inbound = prompt-injection hole (ADR-0067 risk #1).
5. Smoke-test one session end-to-end from the phone (send task, get reply,
   confirm terminal shows inbound + "sent").
6. Repeat for the remaining 4 bots/sessions; flip ADR-0067 `proposed` → `accepted`.

## Watch-outs

- **Research preview**: `--channels` syntax may drift between CC releases; pin what
  version the launcher was tested against in the script header.
- Events to a dead session drop silently — if a session crashes, its chat just goes
  quiet. A liveness ping (cron message "still there?") may be worth adding later.
- If 5-bot friction grates, the escalation path is the deferred custom channel
  server with a chat→session routing table (ADR-0067 alternatives) — a natural
  Treadmill capability (one gateway, N agent sessions).

## Key references

- ADR-0067 — `docs/adrs/0067-cc-channels-one-bot-per-session-for-phone-access.md`
- https://code.claude.com/docs/en/channels-reference (protocol, `--channels`, permission relay)
- https://code.claude.com/docs/en/channels.md (overview, setup walkthrough)
- https://github.com/anthropics/claude-plugins-official — `external_plugins/telegram/`
  (README + ACCESS.md: allowlists, DM pairing policies, `TELEGRAM_STATE_DIR`)
