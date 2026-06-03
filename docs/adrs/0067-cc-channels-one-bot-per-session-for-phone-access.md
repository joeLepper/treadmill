# ADR-0067 — Claude Code Channels, one bot per session, for phone access to concurrent sessions

- **Status:** proposed
- **Date:** 2026-06-03

## Context

Joe runs ~5 concurrent long-lived Claude Code sessions on his workstation, all with
permissions bypassed, and wants to interact with them from his phone — send tasks,
read replies, steer work while away from the terminal.

Two Anthropic features exist for this, and the docs contrast them directly:

> Remote Control: You drive your local session from claude.ai or the Claude mobile app.
> Channels: Push events from non-Claude sources into your already-running local session.

**Remote Control** is the purpose-built answer, but in Joe's direct experience (early
adopter) it times out, stops working, and the UX was poor. We treated that as
disqualifying for a daily driver.

**Channels** (research preview, requires Claude Code ≥ 2.1.80) spawns an MCP channel
server *per session*; official plugins exist for Telegram, Discord, and iMessage. The
architecture is event **fan-out, not demux**: a message goes to the one session whose
process owns that bot. There is no routing table keyed on chat/channel, no session
addressing in the protocol, and no per-session config knob in the stock plugins other
than a separate bot token (`TELEGRAM_STATE_DIR` / `DISCORD_STATE_DIR`). On Telegram
specifically, single-bot sharing is not merely unsupported but broken: `getUpdates`
long-polling permits one poller per token (a second gets `409 terminated by other
getUpdates`).

The two preconditions that usually sink Channels — the session must stay alive, and
permission prompts block unattended — are already solved by Joe's existing setup
(long-running sessions, `--dangerously-skip-permissions`).

## Decision

We decided to reach the concurrent sessions from the phone via **Claude Code Channels
with one dedicated bot per session** — Telegram by default — each session launched with
its own bot token and state dir via a small launcher wrapper. Bots are named after
their sessions, so the phone's chat list *is* the session list: one named chat ↔ one
session.

## Alternatives considered

- **Remote Control.** — The purpose-built feature for driving a chosen session from
  the Claude mobile app. **Why rejected:** demonstrated unreliability in Joe's own use
  (timeouts, sessions going unreachable); we will not route a daily workflow through it.
- **One bot multiplexed across sessions (stock plugins).** — A single bot with
  per-chat/per-channel routing. **Why rejected:** impossible as built. Telegram's
  single-poller constraint hard-fails concurrent sessions on one token; the Discord
  plugin exposes no shard or channel→session routing; the channel protocol itself has
  no session-addressing concept.
- **Custom channel server with a routing table** (chat_id → session). — **Deferred**,
  not rejected: it is the correct long-term shape if bot-per-session friction proves
  real, and it is squarely Treadmill-adjacent (one gateway in front of N agent
  sessions). Build only after the stock setup has been lived with.
- **Discord instead of Telegram.** — Equivalent friction (still one bot per session);
  one app with N channels instead of N chats. Held as a weakly-preferred default for
  Telegram (simpler bot creation via BotFather, no developer-portal setup); switching
  later costs little since the one-bot-per-session shape is identical.

## Consequences

### Good
- Clean per-session addressing with zero custom code; replies land in the right chat.
- Composes with the existing always-on, permissions-bypassed setup — true
  away-from-keyboard operation.
- Each session/bot pairing is independent; adding a sixth session is one more bot.

### Bad / trade-offs
- N bots to create and N tokens to manage; BotFather setup is manual per bot.
- Research preview: the `--channels` flag and protocol contract may change.
- No delivery guarantee: events to a closed/dead session are dropped silently.

### Risks
- **Prompt injection surface.** Inbound remote messages into bypassed-permission
  sessions is the worst-case combination. Mitigation: strict sender allowlisting
  (gate on sender identity, not chat identity) is mandatory, non-negotiable.
- Bun runtime becomes a workstation dependency (plugin scripts require it).

## References

- https://code.claude.com/docs/en/channels-reference
- https://code.claude.com/docs/en/channels.md
- https://github.com/anthropics/claude-plugins-official (telegram/, discord/ plugins)
- Handoff: `docs/handoffs/2026-06-03-cc-channels-one-bot-per-session.md`
