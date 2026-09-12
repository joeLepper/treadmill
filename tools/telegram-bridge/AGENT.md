# telegram-bridge — agent notes

Sole Telegram `getUpdates` poller for the fleet (ADR-0106). Replaces per-session
Telegram MCP pollers, which share one bot token and 409-collide on Telegram's
single-poll-slot limit — the fleet-wide flap. See `bridge/README.md` for the
full design.

## Key surfaces

- `bridge/poller.mjs` — the long-poll loop; all I/O injected for `node --test`.
- `bridge/inject.mjs` — the inbound seam: atomic `.md` drop into
  `~/.cc-channels/<label>/relay/`, injected by the target's `treadmill-events`
  channel server (`tools/cc-channel-treadmill/relay-inbox.ts`).
- `bridge/routing.mjs` — chat_id → label (also the fail-closed allowlist).
- `bridge/ledger.mjs` — durable offset + `update_id` dedup.
- `systemd/telegram-bridge.service` — systemd-user unit; token from a 0600 env
  file, never committed.

## Pitfalls

- **Never two pollers.** Per-session Telegram MCPs must be off before this
  daemon starts, or the 409 flap continues. The cutover is sequenced and
  Alan-signed (high blast radius).
- **Target must run the watcher.** The relay-dir inject only reaches a session
  that runs `treadmill-events` (launcher sessions do). A pure-fabric agent's
  relay dir is unwatched — the "cc-relay retired" case in the treadmill
  CLAUDE.md.
- **Relay `.md` in the base dir, not a subfolder.** `treadmill-events.ts`
  watches the base dir plus a hardcoded subfolder set (`coord`, `worker`) — a
  new `telegram` subfolder would not be watched.
- **fs.watch reads on creation.** inject writes to a `.tmp` then renames, so the
  watcher never sees a half-written file.

## Verified

Seam proven live 2026-09-12: a self-inject into `treadmill-carla`'s relay dir
was consumed by the running channel server and surfaced as
`<channel source="relay">` in-session.

## Related

ADR-0106 (this decision), ADR-0067 (per-session bots, amended), ADR-0102
(fran-bridge — the relay-session precedent this deliberately does NOT copy for
native cc-channels targets), ADR-0093 (effectively-once ledger discipline).
