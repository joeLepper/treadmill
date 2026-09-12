# fran-bridge — Codex sibling (Fran) messaging bridge

Implements ADR-0102: Fran, a Codex sibling, joins the fleet messaging bus as a
bidirectional peer. This directory is the **source of record** for the bridge
code and the systemd substrate. The live runtime deploys under `~/fran/`
(runtime data — the dedup ledger and the outbox spool — stays out of the repo).

## Layout

| Path | Role |
|---|---|
| `bridge/inbound.mjs` | peer→Fran receiver. Durable dedup ledger + commit-on-`task_complete` observer CAPABILITY; the shipped bridge runs it fire-and-forget (one-shot `deliver`, no observer). See Contract. |
| `bridge/inbound.test.mjs` | 11 tests, including child-process crash-recovery. |
| `bridge/deliver-inbound.mjs` | deterministic wrapper: derives a `dedupKey` from `(from,text)`, calls `inbound.mjs deliver`. |
| `bridge/outbound-next.mjs` | Fran→peer. `peek` next spooled outbox message; `ack <id>` moves it to `outbox/sent/`. |
| `bridge/msg-server.mjs` | Fran's outbound MCP stdio server. `send_message({to,text})` atomic-writes to the outbox; `list_peers()`. Fran's Codex (`fran.service`) spawns it, NOT the relay. |
| `bridge/fran-msg.config.toml` | the `[mcp_servers.fran_msg]` stanza for `~/.codex/config.toml`. |
| `bridge/INBOUND.md`, `bridge/README.md` | interface + status docs. |
| `bridge-session/CLAUDE.md` | the `cxbridge` relay session spec (a thin, stateless relay), loaded at session start. |
| `bridge-session/fran-bridge-supervise.sh` / `-reap.sh` | supervise/reap the `cxbridge` tmux relay session. |
| `session/fran-supervise.sh` / `fran-reap.sh` | supervise/reap Fran's own `fran` Codex tmux session. |
| `session/auth-guard.sh` | ExecStartPre gate: ChatGPT auth only, no `OPENAI_API_KEY`. |
| `session/AGENTS.md` | Fran's Codex operating brief (her `~/fran/AGENTS.md`). |
| `peers.json` | the outbound destination allowlist. Code resolves it at `bridge/../peers.json`, so it must sit here at the top level, beside `bridge/`. |
| `systemd/fran.service`, `systemd/fran-bridge.service` | the two user units. |

## Deploy

The scripts reference absolute `/home/joe/fran/...` paths (the live runtime
home). To update the running bridge, copy the changed file to its `~/fran/`
counterpart, then take the action for that file. Two units own different files;
a blanket restart is wrong.

| Changed file | Copy to | Action |
|---|---|---|
| `bridge/inbound.mjs`, `bridge/deliver-inbound.mjs`, `bridge/outbound-next.mjs` | `~/fran/bridge/` | none — the relay shells out `node bridge/<x>.mjs` per drain, so it re-reads the file on the next invocation. |
| `bridge/msg-server.mjs` | `~/fran/bridge/` | `systemctl --user restart fran.service` — Fran's Codex spawns this MCP server; a restart re-registers it. `fran-bridge.service` does NOT own it. |
| `bridge/fran-msg.config.toml` | merge the stanza into `~/.codex/config.toml` | `systemctl --user restart fran.service`. |
| `bridge-session/*` (CLAUDE.md spec + supervise/reap) | `~/fran/bridge-session/` | `systemctl --user restart fran-bridge.service` — a fresh relay session re-reads the spec. |
| `session/*` (fran-supervise, auth-guard, fran-reap, AGENTS.md) | `~/fran/` (top level) | `systemctl --user restart fran.service`. |
| `peers.json` | `~/fran/peers.json` (top level) | none strictly — `outbound-next`/`msg-server` re-read it per call; a `fran.service` restart guarantees it. |
| `systemd/*.service` | `~/.config/systemd/user/` | `systemctl --user daemon-reload && systemctl --user restart <unit>` — reload alone does NOT restart a changed unit. |

Runtime data (`~/fran/bridge/ledger/`, `~/fran/outbox/`, `~/fran/.session-uuid`)
is machine-local state; it is NOT tracked here and must not be copied back.

**This manual-copy model is provisional.** It already dropped a file on review
(the first draft omitted `bridge-session/*`). A code-symlink end-state (symmetric
with ADR-0103) is the goal, but it is NOT a ready action — see Known gaps #1.

## Contract — what is wired, and what is not

- **Inbound commit is a LEDGER capability, not yet an always-on observer.**
  `inbound.mjs` gives a durable dedup ledger with exactly-once COMMIT keyed on
  `dedupKey`, committing on the rollout `event_msg/task_complete` signal
  (ADR-0102). But the shipped bridge calls the one-shot `deliver` — a single
  queue submission, not fire-and-forget with recovery. No shipped unit runs
  `serve`/`watch` to observe `task_complete` and drive a bus acknowledgment. So
  today the bridge does one-shot queue submission and recovery is incomplete: a
  crash after a durable `submitted` but before the enqueue leaves zero
  submissions until explicit recovery. The always-on commit observer plus an
  idempotent bus-ack consumer is a TRACKED GAP (`bridge/README.md` follow-ups
  2-3; Fran builds the paused-queue recovery piece).
- **Message identity is content-only today.** `deliver-inbound.mjs` derives
  `dedupKey = sha256(from + "\n" + text)`. Two distinct but identical
  `(from,text)` messages collide, so the bridge suppresses the second while the
  first is `submitted`/`running` or after it commits (ADR-0102
  "intentional-identical-resend" limit c). A source event ID carried across
  retries would fix it.
- **Outbound retries until ack; duplicate delivery is possible.**
  `msg-server.mjs` stamps a `randomUUID` per outbox file; `outbound-next.mjs`
  retries a pending file until it is acked (commit-after-delivery), so a crash
  between SendMessage and ack replays the file. This is at-least-once, NOT
  exactly-once — the `sent/` archive only stops re-peeking an already-acked
  file. Content-stable operation-key dedup for divergent re-execution is
  deferred (`bridge/README.md` follow-up 4).
- **Orphan spool temp files are reaped (ADR-0102).** `send_message` writes
  `<id>.json.tmp`, fsyncs, then renames to `<id>.json`. A crash in that window (a
  daemon restart) left a durable but pump-invisible orphan — silent loss.
  `outbound-next.mjs` now sweeps `*.json.tmp` on each `peek` (`reapOrphans`):
  age-gated (never touches an in-flight write), JSON-validated (a partial write,
  never acked to the caller, is discarded), and promoted with `link()`+`unlink()`
  (an existing `<id>.json` is never clobbered). `msg-server.mjs` also ends the read
  loop cleanly on a stdout error, so a dead parent yields a clean stop, not a
  "Transport closed" crash — and on such a lost response the message is usually
  already spooled, so callers must check the outbox before a retry.
- **Bus address is unstable.** The harness derives the relay's bus name from the
  workdir plus a random suffix. The relay announces its current address on
  startup (see `bridge-session/CLAUDE.md`).

## Known gaps

1. Divergence: the manual-copy deploy invites silent drift (ADR-0103's failure,
   one layer out). A code-symlink fix is BLOCKED, not ready: `msg-server.mjs` and
   `outbound-next.mjs` derive their root from `import.meta.url`, and Node resolves
   a symlink to its canonical target. So symlinking `~/fran/bridge/*` to a
   canonical checkout makes the code read `canonical/peers.json` and write
   `canonical/outbox/` while the relay still monitors `~/fran/outbox/` — the
   outbox splits. The symlink model needs runtime-root configuration separated
   from code location (e.g. an env var or arg for the data root) and tested
   before it is prescribed. Until then, keep the manual-copy deploy.
2. The exactly-once-COMMIT observer plus bus-ack consumer is not wired; the
   bridge is fire-and-forget submission today.
3. Content-only message identity collides on identical `(from,text)`.
4. Outbound operation-key dedup for divergent re-exec is deferred.
5. Scripts hardcode `/home/joe/fran` and `/home/joe/treadmill`, which undercuts
   the `%h` portability the units already provide.

## Note on skills

Fran's skills are shared per ADR-0103 (one versioned canonical source). This
bridge is the messaging path, not the skills path.

## Tests

`bridge/inbound.test.mjs`: 11 tests, including child-process crash-recovery.
Run `node bridge/inbound.test.mjs`.
