---
name: cc-relay
description: Send a message or file to another named Treadmill orchestrator session (treadmill-alan, treadmill-bert, treadmill-carla, treadmill-donna). Use for handoff docs, bug context, or any cross-session coordination that would otherwise need the operator as a manual relay.
user-invocable: true
allowed-tools:
  - Bash(python3 *)
---

# /cc-relay — Inter-session relay

Drops a message or file into another session's relay inbox.
The target's `treadmill-events` channel server watches that inbox and injects
the content as a channel notification on its next read cycle (or immediately
if the session was recently started — it drains the inbox at startup).

**Transport:** pure local file-drop to `~/.cc-channels/<label>/relay/`.
No Telegram, no network, no setup required.

Arguments passed: `$ARGUMENTS`

---

## Usage

```bash
python3 ~/treadmill/tools/cc-channels/cc-relay.py \
  --to treadmill-carla \
  --from treadmill-alan \
  "Handoff: the VLM client work is in ~/medicoder/…"
```

```bash
python3 ~/treadmill/tools/cc-channels/cc-relay.py \
  --to treadmill-bert \
  --from treadmill-alan \
  --file ~/treadmill/docs/handoffs/2026-06-05-something.md
```

### Flags

| Flag | Required | Description |
|------|----------|-------------|
| `--to <label>` | yes | target session label (e.g. `treadmill-carla`) |
| `--from <label>` | no | source label; prepends `[from: <label>]` to the message |
| `--file <path>` | no | send a file's contents as the message body |
| `"message text"` | no | positional message text |

Pass exactly one of `--file` or message text.

Messages longer than 4096 chars are truncated with `[…]`.

---

## When to use

- Handing off context between sessions when the operator isn't available to relay
- Sending a bug-context note to a session that's working on the same area
- Broadcasting a status update to all other sessions

## When NOT to use

- Asking another session to take an action on your behalf — that still needs
  the operator's explicit approval. Relay is read-only context delivery.
- Anything that should go in a git commit or PR — write it there instead.

---

## How the delivery works

1. `cc-relay.py` writes `~/.cc-channels/<to-label>/relay/<timestamp>.md`
2. The target session's `treadmill-events` channel server (the Bun MCP process
   started by `launch-session.sh`) watches that directory with `fs.watch`
3. When the file appears, the server emits a `notifications/claude/channel`
   with `meta.source = "relay"` — it arrives in the target session's context
   tagged as `<channel source="treadmill-events" ...>`
4. If the target session was offline when the file was written, it drains the
   inbox on its next startup (startup drain)

The relay is one-way. There is no reply mechanism — if you need a response,
the recipient should relay back explicitly.

---

## Dispatch on arguments

Parse `$ARGUMENTS`:
- If empty: print usage (above) and stop.
- If `--to <label> [--from <label>] [--file <path>] ["message"]`: run the
  command exactly, printing the output.

Always prefix the command with `python3 ~/treadmill/tools/cc-channels/cc-relay.py`
unless the script is already on PATH.
