---
auto_merge: false
---

# Plan: Fran — a Codex-harnessed sibling via the App Server

- **Status:** drafting
- **Date:** 2026-09-11
- **Related ADRs:** ADR-0102 (non-Claude-Code sibling via Codex App Server), ADR-0093 (durable messaging), ADR-0095 (named agents bind to hosts), ADR-0073 (persistent sessions), ADR-0070 (review surface)

## Goal

Stand up one experimental Codex sibling, "Fran," reachable through our messaging bus exactly as a Claude Code sibling is — clobber-free, and with the ADR-0093 guarantee preserved past the `codex queue` boundary — per ADR-0102.

## Success criteria

Each is a foil (fails before the change, passes after):

- **Continuity:** give Fran a fact, restart the daemon/host, and she recalls it — the persisted thread UUID is unchanged and its history is non-empty (a same-named empty thread fails).
- **Inbound exactly-once COMMIT (not execution):** kill the daemon between enqueue and turn-completion; the message is **redelivered** (its `dedupKey` was not committed) and the guarantee holds — the `dedupKey` **commits exactly once** (its ledger state transitions once) and the message is **never lost**. Execution count is **recorded as an observation** that characterizes restart-survival (an enqueued-but-unexecuted turn may or may not survive — if it does, the honest outcome is two executions + one commit, which the ADR accepts); it is **not** asserted `==1`. Asserting `==1` would red a compliant build and force in the cancel/fence the ADR scoped out.
- **Order + operator-in-flight:** queue A then B before A completes → processed in order, once each; queue a peer message while the operator is mid-compose → his turn completes intact and his composer is undisturbed.
- **Outbound:** Fran's MCP `send` reaches a named peer; an operator-directed reply does **not** appear on the bus (leak-prevention assertion).
- **Auth:** the serving daemon's **effective account** is subscription mode; a unit or daemon started with `OPENAI_API_KEY` set is caught by the guard.
- **First-class label:** `from=fran` renders as a label wherever the log is folded (dashboard rows, ADR-0070 review queue, agent enumeration).
- **Skill precedence (pins the ADR-0103 fleet-repoint assumption):** place a divergent project-level `./.claude/skills/<name>` in a worktree, and confirm a session there still loads the **canonical** version via the personal `~/.claude/skills/<name>` symlink — proving Personal shadows Project by a marker test, not just the Claude Code guide's word. If Project were to win, the whole fleet-repoint inverts.

## Constraints / scope

### In scope
One Fran instance on one host; the inbound bridge (deferred-commit receiver + `codex queue`), the outbound MCP tool, supervision + auth hardening, and a runbook. **v1 is unicast-only.**

### Out of scope
Replacing or reducing Claude Code. A general multi-harness framework. Multi-host or migrating Fran. **Channel/broadcast delivery** (needs ADR-0093's composite `(subscriber, broadcast_id)` key). Fran as a coordinator owning Treadmill bookkeeping. External connectors. Fran merging production code unattended.

### Budget
~4 focused days, direct-sprint (operator's exec-in-charge, not dispatched). Abort and write a post-mortem if the App Server cannot hold a session, `codex queue` semantics shift, or **the ADR-0093 receiver library cannot defer commit to turn-completion** (that would sink the effectively-once guarantee).

## Sequence of work

1. **Session substrate + auth guard.** systemd + tmux for `codex` as name `fran`: `env -u OPENAI_API_KEY`, trust-dir preconfigured, `--sandbox workspace-write`, attached to the daemon. Ship the auth guard **here** (fail if the unit or the serving daemon's effective account is API-key mode). Verify restart-continuity foil.
2. **Prereq check + inbound bridge.** First confirm the ADR-0093 receiver library exposes a **deferrable commit** (blocks the rest if not). Then the receiver for `fran`: handler calls `codex queue`, commits `dedupKey` only after the turn is durably accepted (reconcile against the thread if the ack does not prove durability). Foil: daemon-crash redelivery. Depends 1.
3. **Ordering foils.** Rapid A/B order-and-once; queued-vs-operator-in-flight. If codex cannot guarantee either, narrow the ADR-0102 claim. Depends 2.
4. **Outbound MCP tool.** Explicit recipient, no auto-forward; leak-prevention test. Depends 1.
5. **Identity + first-class label.** Bind `(fran, host)` (ADR-0095); with the dashboard/review-surface owner, confirm `from=fran` renders everywhere the log is folded. Depends 2, 4.
6. **Harden + runbook.** Usage-limit mid-turn stall, workspace-write trust review, audit logging, the ADR-0102 falsifier checks. Each code unit ships with an INDEPENDENT adversarial review (the author's own does not count). Depends 5.

## Diagram

See ADR-0102 (sequence diagram; the deferred-commit boundary and the two directional contracts are the contract of intent).

## Risks / unknowns

- Receiver library cannot defer commit → effectively-once cannot cross the queue. Mitigation: unit-2 prereq check; abort trigger above.
- App Server experimental — protocol may shift. Mitigation: pin the Codex version.
- `from=fran` not enumerated as a first-class identity → Fran invisible/mis-ordered on surfaces that assume CC identities. Mitigation: unit 5 with that tree's owner before merge.
- `send-keys` creeps back in. Mitigation: a check that no launch/bridge script sends keys to Fran (ADR-0102 falsifier).

## Decisions captured during execution

- (empty)

## Post-mortem

- (pending)
