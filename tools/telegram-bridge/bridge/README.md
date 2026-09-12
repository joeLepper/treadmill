# telegram-bridge

One daemon that is the sole `getUpdates` poller for every session's Telegram bot
(ADR-0106). It ends the per-session Telegram MCP fragility: each session ran its
own MCP poller on its own bot token, and a mass restart dropped many at once (a
shared-plugin-dir install race plus Claude's ~15-min cached-failure cooldown),
leaving a remote operator unable to reach any session. One supervised process,
decoupled from session restarts, replaces N fragile per-session connections.

> Note: the bots are **not** on a shared token (verified 2026-09-12 — six
> distinct bot ids). The daemon is **multi-token**: it owns all N per-session
> bots and runs one poll loop per token.

## How it works

- **Inbound:** one `getUpdates` loop per bot token. The bot identity IS the
  routing key — each bot belongs to one session — so a message polled from bot X
  is written as a `.md` into session X's relay inbox
  (`~/.cc-channels/<label>/relay/`). That session's `treadmill-events` channel
  server watches the dir and injects the file as a `<channel source="relay">`
  notification. No bridge session, no LLM.
- **Outbound:** a session replies with a direct Bot API `sendMessage` (needs only
  the token — no MCP, no poll slot). No daemon involvement.
- **Delivery is at-least-once**, not exactly-once: the daemon injects then acks,
  and the channel server delivers-then-unlinks; a crash in either window
  redelivers. The `update_id` dedup reduces duplicates but does not eliminate
  them. Duplicates are visible and rare; loss is what we refuse.
- **Fail-closed allowlist:** each bot injects only messages from its configured
  `allowedChats`; any other chat is dropped and logged.
- **Content:** plain text passes through. A media message (photo, voice,
  document, …) from an allowed chat is delivered as a text **stub** with its
  caption (`[photo received via Telegram — this channel relays text only]
  caption: …`), so an operator's non-text message surfaces instead of being
  silently dropped — the channel injects text, so the image itself is not
  viewable in-session. **Every** drop is logged with a reason (`allowlist-miss`,
  `no-chat`, `undisplayable`), and per-bot counters (`injected/dropped/
  quarantined/malformed`) are logged periodically so drops are visible in
  aggregate. Sessions run with
  permissions bypassed, so an ungated inbound message would be code execution.
- **Tokens** are read from the existing `~/.cc-channels/<label>/telegram.env`
  (same UID, mode 0600). No new secret file; the daemon concentrates them only
  at runtime.

## Invariants / guards

- **Target eligibility:** a configured label must be a launcher-managed session
  (a `session-id` record exists under `~/.cc-channels/<label>/`), because
  `launch-session.sh` runs the `treadmill-events` watcher for those. An
  unknown/pure-fabric label is refused at startup — never acked-then-lost in an
  unwatched dir.
- **Bridged-marker cross-check:** a configured label must also carry the
  `telegram-bridged` marker (the ADR-0106 cutover gate that stops its session
  from launching its own poller). Without it the session still polls, so the
  daemon polling that bot would be a second poller on the token → 409. The
  daemon refuses at startup with the fix, rather than a runtime contention loop.
- **Symlink safety:** a symlinked `<label>` or `<label>/relay` is rejected
  (lstat + post-mkdir realpath), so a configured label cannot resolve into
  another session's dir. Same-UID concurrent rewrite (TOCTOU) remains a disclosed
  limit, not a defended boundary — this is a single-UID system.
- **Single instance:** a kernel-held lock — an abstract-namespace unix socket
  keyed on `uid + realpath(stateDir)`, released by the kernel on process death —
  refuses a second daemon (a stray instance would double-poll every token → 409
  on all). No pidfile, so no stale-file or pid-reuse race.
- **Head-of-line:** a failed inject is quarantined (`quarantine/<label>/`) and
  advanced past — one poison update never stalls the rest.
- **Persistent 409** is logged as contention (a per-session poller was not
  disabled), not retried silently forever.
- **Routes are loaded once at startup.** Adding/removing a bot or rotating a
  token requires a daemon restart. Eligibility is checked at startup only, so a
  session torn down *after* start still has messages spooled into its relay dir
  (harmless — the files persist and are drained on that session's next start).
  Offsets are keyed by stable bot id, never by label: rotating a bot's
  SECRET keeps its bot id and correctly RETAINS its ledger; only replacing the
  bot (a new bot id) yields a fresh ledger.
- **Duplicate bot rejected.** Two labels resolving to the same token/bot id is
  refused at startup (it would put two pollers on one bot — 409 + cross-
  delivery); the error names the labels and bot id, never the token.

## Modules

| File | Role |
|------|------|
| `poller.mjs` | per-token `getUpdates` loops + per-bot backoff supervisor + daemon fan-out |
| `config.mjs` | bot list, token load from `telegram.env`, startup eligibility gate |
| `inject.mjs` | atomic, symlink-safe relay-dir file-drop (the inbound seam) |
| `ledger.mjs` | per-bot durable offset + `update_id` dedup (persist-then-mutate) |
| `bridge.mjs` | entrypoint: config + kernel lock (abstract socket) → daemon |

## Run

```bash
mkdir -p ~/.cc-channels/.telegram-bridge
cp bridge/bots.example.json ~/.cc-channels/.telegram-bridge/bots.json   # edit labels + allowedChats
node bridge/bridge.mjs           # or: systemctl --user start telegram-bridge
```

`node --test bridge/` runs the suites (no network — fetch/inject/quarantine are
injected).

## Cutover (Alan signs off — high blast radius)

Never two pollers on one token. A session's per-session Telegram MCP must stop
before this daemon polls that bot, and the session must switch outbound to direct
`sendMessage` (disabling the MCP kills its outbound half too). Sequence
`treadmill-alan` first, verify inbound + outbound end-to-end and that the 409
contention log stays quiet, then fan out. See
`docs/plans/2026-09-12-telegram-bridge-daemon.md`.
