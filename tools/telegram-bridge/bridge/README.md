# telegram-bridge

The sole Telegram `getUpdates` poller for the fleet (ADR-0106). It ends the
fleet-wide 409 flap: the shared bot token permits exactly one `getUpdates`
poll slot, so N per-session pollers collide and knock each other offline. One
daemon owns the slot; sessions no longer poll.

## How it works

- **Inbound:** the daemon long-polls `getUpdates`, resolves each message's
  `chat_id` to a session label via the routing table, and writes the message as
  a `.md` file into that session's channel relay inbox
  (`~/.cc-channels/<label>/relay/`). The session's `treadmill-events` channel
  server watches that dir and injects the file as a `<channel source="relay">`
  notification (at-least-once; `relay-inbox.ts`). No bridge session, no LLM.
- **Outbound:** a session replies with a direct Bot API `sendMessage`. Outbound
  does not use the poll slot, so it never contends.
- **Effectively-once:** the ledger persists the Telegram `offset` (the ack) and
  a bounded set of injected `update_id`s. A crash re-fetches only the
  un-injected tail; an already-injected update is skipped.
- **Fail-closed:** the routing table doubles as the inbound allowlist. A message
  from an unmapped chat is logged and dropped, never delivered to a wrong
  session (the ADR-0106 falsifier).

## Target eligibility (invariant)

A session is a valid Telegram target only if it runs the `treadmill-events`
channel server — every `launch-session.sh` session does. The treadmill CLAUDE.md
"cc-relay is retired" note applies to pure-fabric agents that run no watcher; it
does not apply to launcher sessions, whose relay dir is actively watched.

## Modules

| File | Role |
|------|------|
| `poller.mjs` | sole `getUpdates` long-poll loop + capped-backoff supervisor |
| `routing.mjs` | chat_id → label table (also the allowlist) |
| `inject.mjs` | atomic relay-dir file-drop (the inbound seam) |
| `ledger.mjs` | durable offset + `update_id` dedup (effectively-once) |
| `bridge.mjs` | entrypoint: env + config → poller |

## Run

```bash
mkdir -p ~/.cc-channels/.telegram-bridge
cp bridge/routing.example.json ~/.cc-channels/.telegram-bridge/routing.json   # edit chat_ids
printf 'TELEGRAM_BOT_TOKEN=%s\n' "$TOKEN" > ~/.cc-channels/.telegram-bridge/telegram.env
chmod 600 ~/.cc-channels/.telegram-bridge/telegram.env
node bridge/bridge.mjs           # or: systemctl --user start telegram-bridge
```

The token stays in a mode-0600 env file, never in the committed tree or the
unit. `node --test bridge/` runs the suites (no network — fetch is injected).

## Cutover (Alan signs off — high blast radius)

Never two pollers at once. Per-session Telegram MCPs must stop before this
daemon starts. Sequence one label first, verify 409 disappears, then fan out.
See `docs/plans/2026-09-12-telegram-bridge-daemon.md`.
