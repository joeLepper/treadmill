# Fran bridge (ADR-0102) — status: v1 WORKING, bidirectional

Makes Fran (the Codex sibling, tmux session `fran`) a bidirectional participant
on the fleet messaging bus. Proven end-to-end 2026-09-11 (two round-trips:
peer→inbound→Fran→reply→relay→peer, both directly and bridge-mediated).

## Components
- **inbound.mjs** (built by Fran) — peer→Fran. Durable dedup ledger
  (`ledger/`, node:sqlite lock + JSON/fsync records), commit on the rollout
  `event_msg/task_complete` signal. Exactly-once COMMIT, at-least-once execution
  — a CAPABILITY driven only when `serve`/`watch` runs; the shipped bridge calls
  one-shot `deliver` (fire-and-forget, observer not wired — see AGENT.md
  Contract). Interface: `serve` (JSONL) / `deliver` (one-shot) / function API.
  See INBOUND.md. 11 tests incl. child-process crash-recovery.
- **deliver-inbound.mjs** — deterministic wrapper the bridge session calls per
  inbound message: derives a stable dedupKey from (from,text), hands to
  `inbound.mjs deliver`.
- **outbound-next.mjs** — Fran→peer. `peek` returns the next spooled outbox
  message; `ack <id>` moves it to `outbox/sent/`. The bridge SendMessages, then
  acks (commit-after-delivery = at-least-once outbound; a crash before ack
  replays, so duplicate delivery is possible).
- **../bridge-session/CLAUDE.md** — the `fran-bridge` Claude Code session's spec:
  a thin relay. Outbound: monitor `~/fran/outbox`, drain via outbound-next.
  Inbound: run deliver-inbound per incoming message.

## How to reach Fran (current)
Siblings `SendMessage <bridge bus name>` — currently **`bridge-session-b3`**
(unstable; see follow-ups). The bridge relays it into Fran; her reply comes back
out through the bridge as `[from Fran] ...`.

## Run / recover — systemd-supervised (best-effort)
`fran-bridge.service` (user unit) supervises the relay session via
`bridge-session/fran-bridge-supervise.sh`: it launches a FRESH session each start
(the relay is stateless), answers the folder-trust prompt, and re-kicks it (re-arm
the Monitor + drain + relay-only). Survives reboot (WantedBy=default.target) and
restarts on failure.
    systemctl --user restart fran-bridge.service   # clean restart
Caveats (best-effort, not bulletproof): a clean start/restart works; **rapid
back-to-back restarts race** (two supervisors fight over the tmux session — stop,
wait, start). The Monitor is re-armed by the kick each start.

## Follow-ups (v1 → hardened)
1. **Bus name is unstable** — the harness derives it from the workdir + a random
   suffix, and it changes on every launch (verified: b3 → 0e → f9 → 28; NOT from
   `TREADMILL_SESSION_LABEL`). MITIGATED, not fixed: on startup the bridge
   ANNOUNCES its current address to the orchestrators (CLAUDE.md step 2), so
   siblings learn where to reach Fran from the announce's `from` field. A truly
   stable name needs the real harness name-lever (unknown) or the sanctioned
   treadmill-channel@ launch (which risks team-detection on the label).
2. **systemd persistence + auto-re-arm.** Supervise the session (survive reboot)
   AND re-arm the outbox Monitor on each (re)start — the Monitor dies with the
   session process. May warrant a non-LLM daemon design (deterministic, cheap)
   IF the cc-socks wire protocol can be spoken directly.
3. **Integration foils:** live queue envelope fidelity — RUN, PASS (round-trips
   verified it). **Daemon-restart survival** — an enqueued-but-unexecuted `codex
   queue` turn does NOT auto-execute after an app-server restart: Codex pauses
   the recovered queue. Paused is NOT absent — a restart is not proof the turn
   is gone. Recovery requires explicit handling (resume/start the pending entry
   where supported, otherwise an authorized replay that accepts duplicate-execution
   risk); it must never treat a restart as proof of absence. Recovery is
   incomplete; Fran builds the paused-queue piece.
4. **Outbound operation-key dedup** for divergent-re-exec (needs send_message to
   stamp a stable op id) — v1 retries per outbox file until ack, so a crash
   before ack replays: duplicate delivery is possible (at-least-once, not once).
