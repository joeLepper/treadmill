# telegram-bridge — agent notes

Sole `getUpdates` poller for every session's Telegram bot (ADR-0106). Replaces N
fragile per-session Telegram MCP pollers. The bots are on DISTINCT tokens (not a
shared token — verified 2026-09-12), so the daemon is MULTI-TOKEN: one poll loop
per bot, each bound to its session. See `bridge/README.md` for the full design.

## Key surfaces

- `bridge/poller.mjs` — per-token `getUpdates` loops; all I/O injected for
  `node --test`. `runDaemon` fans out; each `runBot` is supervised independently.
- `bridge/inject.mjs` — the inbound seam: atomic, symlink-safe `.md` drop into
  `~/.cc-channels/<label>/relay/`, injected by the target's `treadmill-events`
  channel server (`tools/cc-channel-treadmill/relay-inbox.ts`). Also
  `isWatcherEligible` (the launcher `session-id` gate).
- `bridge/config.mjs` — bot list `{label, allowedChats}`, token load from
  `telegram.env`, startup eligibility gate.
- `bridge/ledger.mjs` — per-bot durable offset + `update_id` dedup
  (persist-then-mutate).
- `systemd/telegram-bridge.service` — systemd-user unit; NO token in the unit
  (each bot's token is read from its `telegram.env`).

## Pitfalls

- **Never two pollers on one token.** A session's per-session Telegram MCP must
  be off before the daemon polls that bot; a persistent 409 logs as contention.
  Cutover is sequenced + Alan-signed. Disabling a session's MCP kills its
  outbound half too — the session must switch outbound to direct `sendMessage`.
- **Target must run the watcher.** The relay-dir inject only reaches a launcher
  session (has a `session-id` record). A pure-fabric label is refused at startup
  — the "cc-relay retired" case in the treadmill CLAUDE.md.
- **At-least-once, not exactly-once.** inject-then-ack + the channel server's
  own deliver-then-unlink both permit redelivery. Duplicates are accepted; loss
  is the falsifier.
- **Relay `.md` in the base dir, not a subfolder.** `treadmill-events.ts`
  watches the base dir plus a hardcoded subfolder set (`coord`, `worker`).
- **fs.watch reads on creation.** inject writes to a `.tmp` then renames.

## Verified

Seam proven live 2026-09-12: a self-inject into `treadmill-carla`'s relay dir
(via the multi-token `injectToRelay`) was consumed by the running channel server
and surfaced as `<channel source="relay">` in-session.

## Related

ADR-0106 (this decision), ADR-0067 (per-session bots, amended), ADR-0102
(fran-bridge — the relay-session precedent this deliberately does NOT copy for
native cc-channels targets), ADR-0093 (effectively-once ledger discipline).
