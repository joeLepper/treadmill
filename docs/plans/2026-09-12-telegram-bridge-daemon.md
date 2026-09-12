# Plan: Telegram bridge daemon (ADR-0106)

- **Status:** active
- **Date:** 2026-09-12
- **Related ADRs:** ADR-0106 (persistent Telegram bridge daemon), amends ADR-0067; models on ADR-0102 (fran-bridge)

## Goal

End the fleet-wide Telegram flapping (shared bot token → one `getUpdates` slot → concurrent
session-pollers 409-collide) by making a **single persistent poller** own the slot and fan inbound
messages to sessions over the fabric, with outbound via the Bot API. Model on the existing fran-bridge
rather than build a divergent daemon.

## Success criteria

- Exactly **one** `getUpdates` poller runs against the token fleet-wide; no session runs a Telegram MCP
  poller. (Observable: `409 Conflict` disappears from logs; the connection stops flapping.)
- An inbound Telegram message addressed to session X is delivered to X's fabric inbox and **only** X's
  (routing table chat_id → session), effectively-once (dedup ledger, per fran-bridge/ADR-0093).
- A session sends an outbound reply and it reaches the operator's Telegram chat.
- A poller drop is auto-recovered by systemd + backoff without operator intervention (the ADR-0106
  falsifier does not fire).

## Constraints / scope

### In scope
- A sole-poller service (systemd-user, supervised) owning `getUpdates`.
- Inbound fan-out to sessions via the fabric, reusing the fran-bridge delivery + dedup ledger pattern.
- Outbound send via the Bot API (direct `sendMessage` — already proven to bypass the poll slot).
- A routing table (chat_id ↔ session label) with registration as sessions come/go.
- The **cutover**: disable per-session Telegram MCP pollers, then start the bridge poller (atomic-ish —
  never two pollers at once, never zero inbound for long).

### Out of scope
- Discord/iMessage bridges (Telegram first; the shape generalizes later).
- Replacing the fabric or fran-bridge; this reuses both.

### Budget
~1–2 days. Abort/rethink if the fran-bridge fabric-injection seam can't be reused for the
external→Claude-session direction (then escalate the seam design to Alan, the fabric owner).

## Sequence of work

1. **Confirm the seam. [done — 2026-09-12]** Verified against `launch-session.sh`,
   `treadmill-events.ts`, and `relay-inbox.ts`. Inbound inject = write a `.md` into
   `~/.cc-channels/<label>/relay/`; the session's `treadmill-events` channel server watches that dir and
   injects it as `<channel source="relay">` (at-least-once notify-then-unlink, 60s resweep). No bridge
   session — the targets run the watcher (unlike fran-bridge's Codex targets). Alan confirmed the design
   and assigned build ownership to Carla; review = Gerald + Fran cross-model + Alan co-sign; Alan signs
   the cutover.
2. **Sole-poller service.** `getUpdates` long-poll, offset-tracked, config-driven token, systemd-user
   unit with restart + backoff. Standalone-testable (polls, prints messages). The self-contained core
   that directly ends the 409 flap.
3. **Routing + inject.** chat_id → session label table; each inbound message written as a `.md` into the
   target session's relay dir, through a dedup ledger (per fran-bridge/ADR-0093, keyed on Telegram
   `update_id`). A target is valid only if it runs the `treadmill-events` watcher.
4. **Outbound.** Sessions keep direct-API `sendMessage` outbound (already works, bypasses the poll slot).
   No daemon involvement needed — confirmed during seam analysis.
5. **Cutover.** Disable per-session Telegram MCP (stops the colliding pollers), then start the bridge
   poller. Sequenced so there is never a second poller and inbound gaps are brief. One label first,
   verify, then fan out (Alan's sequencing; the 2026-09-12 12:32 tmux churn showed restarts have
   collateral — no blind fleet restarts). Alan signs off.
6. **Verify** against the success criteria; watch for 409 disappearing + no flapping.

## Risks / unknowns

- The fabric-injection seam (external→Claude-session) may need Alan's help — step 1 gates the build.
- Cutover blast radius: touches every session's Telegram config. Mitigate by sequencing + coordinating,
  not a fleet-wide restart.
- A routing bug mis-delivers a message — covered by the ADR-0106 falsifier + per-session dedup.

## Decisions captured during execution

- Outbound `sendMessage` bypasses the contended poll slot (proven 2026-09-12 — operator updates got
  through via direct Bot API while inbound flapped). So the hard part is inbound only.
- Inbound uses the relay-dir file-drop, NOT a bridge Claude session (Alan, 2026-09-12). A plain daemon
  cannot call the `SendMessage` harness tool and there is no `send` CLI, so the relay-dir inject is the
  only push a plain daemon has into a session. Verified live for launcher sessions.
- The "cc-relay retired" note in the treadmill CLAUDE.md is scoped to pure-fabric agents that run no
  watcher; launcher sessions run `treadmill-events` and watch the relay dir. Target eligibility =
  "runs the watcher". Recorded as an ADR-0106 invariant.
- Build ownership: Carla owns; Alan reviews via Gerald + Fran (two cross-model passes, ADR-0105) + his
  co-sign; Alan signs the cutover.

## Post-mortem

- (pending)
