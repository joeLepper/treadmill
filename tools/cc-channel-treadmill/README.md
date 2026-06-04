# cc-channel-treadmill — Treadmill events channel for Claude Code

Pushes Treadmill dispatched-work lifecycle events into the originating Claude
Code session (ADR-0068), replacing per-session poll monitors. One-way channel:
the session reads `<channel source="treadmill-events">` events and acts.

> **Telegram access management (multi-bot caveat):** the launcher gives each bot
> its own `TELEGRAM_STATE_DIR` under `~/.cc-channels/<label>/telegram/` — mandatory,
> because the telegram plugin keeps a `bot.pid` singleton guard in the state dir, so
> two bots sharing a dir would kill each other. The stock `/telegram:access` skill
> hardcodes the single default path `~/.claude/channels/telegram/`, so it silently
> edits the wrong file under this layout. **Manage access with
> `tools/cc-channels/cc-access.py --label <label> <status|pair CODE|policy allowlist|...>`
> instead** — it targets the per-session `access.json` the server actually reads.
> Each bot is a one-time `pair` + `policy allowlist`; both persist across relaunches.

## How it routes (ADR-0068)

- The session's **label** (`TREADMILL_SESSION_LABEL`, set by
  `tools/cc-channels/launch-session.sh`) must equal the `--created-by` the
  session passes on `treadmill plan submit`.
- The server connects to `WS /api/v1/dashboard/ws/events?created_by=<label>`
  (server-side filter, ADR-0068 step 1) **and** enforces ownership client-side
  against a reconciled set of the label's plan/task ids — correct both before
  the filter lands and as defense-in-depth after.
- **Reconcile-on-connect:** every (re)connect emits one `catch_up="true"`
  summary of the label's active tasks — a restarted session must not trust
  silence.
- **Dedup** by event id (SQS redelivery), sliding window of 500.

## Setup (one-time)

1. Install [Bun](https://bun.sh), then from this directory: `bun install`.
2. Register the server **user-level** (sessions run in many repos) in
   `~/.claude.json` under `mcpServers`, absolute path:

   ```json
   "treadmill-events": {
     "command": "bun",
     "args": ["/home/joe/treadmill/tools/cc-channel-treadmill/treadmill-events.ts"]
   }
   ```

3. Launch sessions via `tools/cc-channels/launch-session.sh <label>` — it sets
   the label env and passes `--dangerously-load-development-channels
   server:treadmill-events` (custom channels are allowlist-gated during the
   research preview; the flag's bypass is per-entry and does NOT extend to
   `--channels` entries).

## Smoke test

1. `launch-session.sh smoke-test ~/treadmill`
2. In the session: `/mcp` should show `treadmill-events` connected
   ("Failed to connect" → check `~/.claude/debug/<session-id>.txt`).
3. Submit a trivial plan with `--created-by smoke-test`; lifecycle events
   should arrive as `<channel source="treadmill-events" entity_type=...>`
   without any polling.

## Env

| Var | Default | Meaning |
| --- | --- | --- |
| `TREADMILL_SESSION_LABEL` | (required) | session label = `created_by` routing key |
| `TREADMILL_API_URL` | `http://localhost:8088` | Treadmill API base — must be the direct API port; the `:8080` auth proxy serves REST but does not upgrade WebSockets |
| `TREADMILL_API_KEY` | `BUNKHOUSE_API_KEY` | Bearer for REST + WS |

Pinned against Claude Code 2.1.161; channels are a research preview — re-verify
the flag contract after CC upgrades.
