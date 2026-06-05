---
auto_merge: true
status: drafting
---

# Plan: cc-relay trust gates — distinguish context from action relay

- **Status:** drafting
- **Date:** 2026-06-05
- **Related ADRs:** ADR-0067 (phone-access channels), ADR-0068 (treadmill-events channel)

## Goal

The inter-session relay landed as pure file-drop with no enforcement on what
a receiving session does with the content. Today a relay message can request
arbitrary shell commands and a session may comply — Bert correctly flagged
this after restarting Alan and Carla/Donna via relay-injected commands. We
need a two-tier message model: context delivery (free-form, always safe) vs.
action requests (explicitly tagged, require operator confirmation or a
pre-authorized trust policy before execution).

## Success criteria

1. `cc-relay.py` accepts a `--type context|action` flag (default: `context`).
   Action-typed messages are prefixed with a visible `[ACTION REQUEST]` header
   in the relay file so the receiving session can recognize them unambiguously.
2. The `/cc-relay` skill doc is updated: action-typed messages require the
   receiving session to either (a) obtain explicit operator confirmation before
   executing, or (b) consult a per-label trust policy file before acting.
3. A trust policy file `~/.cc-channels/<label>/relay-trust.json` can
   pre-authorize specific source labels for action relay without per-message
   confirmation. Absent the file, ALL action-typed relay messages require
   operator confirmation.
4. `cc-relay.py` and the skill doc are covered by the updated test suite
   (type flag written to file, header present in output).
5. `tools/cc-channels/AGENT.md` updated: trust model documented in Pitfalls.

## Constraints / scope

### In scope
- `tools/cc-channels/cc-relay.py` — add `--type` flag
- `tools/cc-channels/tests/test_cc_relay.py` — cover `--type` behavior
- `.claude/skills/cc-relay/SKILL.md` — add trust-gate guidance
- `tools/cc-channels/AGENT.md` — pitfalls update
- `relay-trust.json` schema definition (documented; no code reads it yet — that's step 2)

### Out of scope
- Cryptographic signing of relay messages (overkill for a same-host channel)
- Automatic enforcement in `treadmill-events.ts` (the server delivers; the
  session decides — enforcement lives in session judgment, not the transport)
- Rate limiting or quota on relay volume
- Reading/enforcing `relay-trust.json` in code (documented contract first;
  automated enforcement is a follow-up once sessions demonstrate they read it)

### Budget
One focused session; two small PRs at most.

## Sequence of work

1. **`--type` flag + test coverage** — add `--type context|action` to
   `cc-relay.py`. Action messages prepend `[ACTION REQUEST]\n\n` to the body.
   Tests: verify flag is written to file, header present, default is `context`.
   Scope: `cc-relay.py`, `test_cc_relay.py`.

2. **Skill + AGENT.md update** — update `/cc-relay` SKILL.md with explicit
   guidance: receiving sessions treat `[ACTION REQUEST]` messages as requiring
   operator confirmation unless `relay-trust.json` pre-authorizes the source
   label. Document the `relay-trust.json` schema in AGENT.md pitfalls.
   Scope: `.claude/skills/cc-relay/SKILL.md`, `tools/cc-channels/AGENT.md`.

Steps 1 and 2 can land in a single PR — scope is small and they're tightly
coupled.

## Risks / unknowns

- Sessions may ignore the `[ACTION REQUEST]` header regardless of skill doc
  guidance. Automated enforcement in `treadmill-events.ts` (rejecting
  action-typed messages unless the trust file is present) is a stronger
  backstop, deferred to avoid over-engineering before we understand the
  real failure modes.
- The two-tier model adds friction to legitimate cross-session coordination.
  The `relay-trust.json` pre-authorization path is the escape valve — once
  sessions establish mutual trust, action relay becomes frictionless again.

## Decisions captured during execution

_Empty — populated as we work._

## Post-mortem

_Not yet._
