# Plan: Telegram bridge daemon (ADR-0106)

- **Status:** active
- **Date:** 2026-09-12
- **Related ADRs:** ADR-0106 (persistent Telegram bridge daemon), amends ADR-0067; models on ADR-0102 (fran-bridge)

## Goal

End the per-session Telegram MCP fragility (a mass restart drops many sessions' MCP pollers at once — a
shared-plugin-dir install race + Claude's ~15-min cached-failure cooldown — leaving a remote operator
unable to reach any session) with a **single persistent, multi-token daemon**: it owns all N per-session
bots (distinct tokens, verified — not a shared token), runs one `getUpdates` loop per bot, and injects
inbound into each session's relay dir. Outbound stays direct Bot API. Model on the existing fran-bridge
conventions rather than build a divergent daemon.

## Success criteria

- Exactly **one** process polls each bot token; no session runs a Telegram MCP poller. (Observable: no
  `409 Conflict` contention in the daemon log; connections stop flapping.)
- An inbound Telegram message from an allowed chat on bot X is injected into session X's relay dir and
  **only** X's (the bot identity is the routing key), and consumed as `<channel source="relay">`.
  Delivery is **at-least-once** (a duplicate is acceptable; a drop or mis-delivery is the falsifier).
- A session sends an outbound reply via direct `sendMessage` and it reaches the operator's Telegram chat.
- A per-bot poll drop is auto-recovered by the loop's backoff without operator intervention (the ADR-0106
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
2. **Multi-token poller service. [done]** One `getUpdates` loop per bot, offset-tracked, tokens read from
   each session's `telegram.env`, systemd-user unit with per-bot backoff + pid-lock. Standalone-testable.
3. **Inject + eligibility. [done]** Each loop is bound to its session label (bot identity = routing key);
   each inbound allowed message is written as a `.md` into that session's relay dir through a per-bot
   dedup ledger (`update_id`). A target is validated at startup as a launcher-managed session; a
   disallowed chat is dropped fail-closed; a failed inject is quarantined, not stalled.
4. **Outbound. [done]** Sessions use direct-API `sendMessage` (bypasses the slot). Disabling a session's
   MCP at cutover kills its outbound half, so the session must switch to direct send — noted for cutover.
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
- **Root cause corrected (2026-09-12).** The initial "shared bot token → one `getUpdates` slot → 409"
  diagnosis was falsified by ground truth: the six session bots have distinct tokens (verified by hash),
  a live probe showed the slot free (no 409). The real cause is per-session MCP fragility that correlates
  on a mass restart. The daemon still fixes it, but the design pivoted to **multi-token** (own all N
  bots, one loop each, route by bot→session; no chat_id table). ADR-0106 Context/Decision corrected.
- **First-round review findings folded (Fran + Bert, 2026-09-12), before the multi-token push:** name it
  at-least-once (not effectively-once); startup eligibility gate (`session-id` record) so an unwatched
  target is refused not acked-then-lost; symlink-safe relay dir (lstat + realpath) with a disclosed
  same-UID TOCTOU limit; per-update quarantine so a poison update never stalls the batch; pid-lock the
  state dir (a stray instance would double-poll); ledger persist-then-mutate; persistent-409 surfaced as
  contention; routes loaded once (restart to change). Re-route to Fran + Gerald + Bert after the push.

## Post-mortem

- (pending)
