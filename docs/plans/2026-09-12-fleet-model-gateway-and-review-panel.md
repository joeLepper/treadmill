# Plan: Fleet model gateway + cross-model review panel

- **Status:** active
- **Date:** 2026-09-12
- **Related ADRs:** ADR-0107 (fleet model gateway), ADR-0105 (cross-model review, two passes), ADR-0104 (Gerald / open-weight)

## Goal

Build the fleet model gateway ADR-0107 decided, then a `panel` tool on top so any
agent can, during an ADR / plan / gate, fan a review artifact to a panel of
cross-family models and collect ranked verdicts in one command. The gateway is the
routing + policy plane; the panel is the ergonomic primitive that uses it.

**Multi-provider routing (operator directive 2026-09-12):** the panel spans THREE
families — open-weight (Qwen/GLM/Kimi/MiniMax), GPT, and Claude. Routing:
- open-weight -> gateway -> OpenCode Go (the Go budget, reserved for these).
- GPT -> a headless OAuth session via the Codex CLI (NOT an API key; the OAuth we
  already pay for). No Go budget spent on gpt-*.
- Claude -> a Claude Code subagent / headless `claude -p` (the subscription OAuth,
  NOT an Anthropic API key).
The OpenCode Go budget must go ONLY to models we do NOT otherwise have access to;
the gateway must NEVER route gpt-*/grok-* even though Go's catalog can serve them.

**Gateway scope decision (v1):** because GPT and Claude route via OAuth CLIs OUTSIDE
the gateway, the gateway fronts ONLY the open-weight leg. v1 runs LiteLLM in
CONFIG-mode with an open-weight-only `model_list` (the list IS the allowlist;
anything else 404s) — no Postgres, no per-caller virtual keys. Per-caller virtual
keys + budgets (the ADR-0107 DB-mode spike) become a governance follow-up once more
than one caller needs distinct policy. Go's own per-model dollar caps bound spend
for v1. Rationale: avoid a persistent Postgres failure surface for a single-caller
v1 while still delivering "never proprietary via Go" and a real routing point.

## Success criteria

1. A persistent gateway exposes ONE OpenAI-compatible endpoint serving ONLY the
   open-weight models. A request for an allowlisted open-weight model succeeds; a
   request for a proprietary model (e.g. `gpt-5.6-luna`, `grok-4.6`) is refused
   (HTTP 400) and no Go budget is spent on it.
2. `panel review --artifact <path>` fans out across THREE families in one command —
   open-weight (Qwen/Zhipu/Moonshot/MiniMax via the gateway), GPT (`codex exec`
   OAuth), Claude (`claude -p` subscription) — and prints each reviewer's VERDICT +
   findings, ranked, plus a synthesis.
3. Reasoning is not leaked: MiniMax `<think>...</think>` and `<reasoning>` blocks
   (including a dangling open tag) are stripped; an empty-on-budget model is reported
   `no-output`, never a silent empty approval.
4. The gate FAILS CLOSED: exit 0 only on a non-block verdict WITH a cross-family
   quorum; block, a fully-degraded `no-verdict`, or a lone surviving reviewer all
   exit non-zero. A malformed or inline-quoted verdict does not parse as approval.
5. No API keys for the paid legs: GPT uses Codex OAuth, Claude uses the subscription
   (`ANTHROPIC_API_KEY` unset). Untrusted-artifact safety: both paid legs run
   sandboxed (codex `-s read-only`; claude with execution/exfil tools disallowed).
6. Both components ship on a branch with review before merge: the panel dogfoods
   itself as the cross-model pass (ADR-0105) plus a sibling co-sign.

## Constraints / scope

### In scope
- LiteLLM gateway (CONFIG-mode) fronting OpenCode Go's OPEN-WEIGHT models only, as a
  persistent user service with its own venv, systemd unit, and a master key. The
  `model_list` IS the allowlist; no router fallbacks are configured.
- A `panel` CLI spanning THREE families: open-weight via the gateway; GPT via
  `codex exec` (Codex OAuth, isolated minimal home); Claude via `claude -p` (the
  subscription). Strips reasoning, parses verdicts, ranks, fails closed with a
  cross-family quorum, emits JSON + a human digest.

### Out of scope
- Routing proprietary models THROUGH the gateway / spending Go budget on gpt/grok
  (Go budget is for open-weight only; GPT/Claude come from their own OAuth).
- DB-mode per-caller virtual keys + budgets (the ADR-0107 spike) — a GOVERNANCE
  FOLLOW-UP, deferred while there is a single caller; Go's own caps bound spend.
- deepseek-v4-pro (China-region-gated) and grok (proprietary) in the panel pool.
- Heavyweight agentic per-model subagents (single-shot calls only here).
- Wiring the panel into the ADR/plan/gate skills as a hard gate (Phase 3, later).
- A hard security sandbox for the fleet (same-UID blast radius stands, per ADR-0107);
  the panel's OWN untrusted-artifact risk IS handled (both paid legs sandboxed).

### Budget
This session. If Phase 1 cannot be made durable and safe within it, stop and write
a post-mortem rather than ship a fragile service.

## Sequence of work

1. **Gateway service** — LiteLLM CONFIG-mode fronting Go open-weight models, own
   venv + systemd unit + chmod-600 secret (master key + Go key); model_list =
   allowlist, no fallbacks. Foil: open-weight served, proprietary HTTP 400.
2. **Panel tool** — `tools/model-review-panel/panel.py`: concurrent three-family
   fan-out (gateway + `codex exec` + `claude -p`), reasoning strip, line-anchored
   verdict parse, cross-family-quorum fail-closed gate, sandboxed paid legs, JSON +
   human output; unit tests (stubbed legs, no network).
3. **Review + deploy** — panel dogfoods itself as the cross-model pass + a sibling
   co-sign; deploy the gateway service; demo on real artifacts (the panel's own code
   and this plan). Open-weight leg verifies live once the OpenCode Go cap clears.

## Risks / unknowns

- **Postgres operational weight.** A persistent DB is a new failure surface. Mitigation:
  supervise + a health check; abort to config-mode (fixed allowlist, no per-caller
  budget) if DB-mode proves fragile, and say so.
- **Reasoning models return empty under a small budget.** Mitigation: the panel sets
  a generous max_tokens and treats empty as `no-output`, never a silent approval.
- **Provider flakiness** (Internal server error on some models). Mitigation: the
  panel degrades — a failed model is reported, the panel still returns on the rest.
- **Same-UID blast radius** (ADR-0107). Accepted, documented; not solved here.
- **We'll abort if** the gateway cannot refuse a proprietary/out-of-allowlist request
  (criterion 1/5) — without that, it is not a governance plane and must not ship.

## Decisions captured during execution

- **v1 gateway is config-mode, not DB-mode** — GPT/Claude route via OAuth CLIs
  outside the gateway, so the gateway fronts only open-weight; a single caller does
  not justify a persistent Postgres. DB-mode virtual keys/budgets are the governance
  follow-up.
- **Panel spans three families via what we already pay for** (operator, 2026-09-12):
  open-weight via the gateway (Go budget), GPT via Codex OAuth, Claude via the
  subscription — no API keys. Go budget reserved for models we cannot get elsewhere.
- **Gerald (persistent open-weight agent) retired** in favour of open-weight REVIEW
  via the panel (operator, 2026-09-12); Go quota goes to reviews.
- **GPT leg needs an isolated minimal CODEX_HOME** — the default home's MCP startup
  pushed it past 240s; a clean home returns in ~9s.
- **Untrusted-artifact safety is mandatory** — the panel reviews untrusted text, so
  both paid legs are sandboxed (codex read-only; claude tools disallowed). Found by
  the panel dogfooding itself.
- **Fail-closed gate with cross-family quorum** — a fully-degraded panel, a lone
  reviewer, or a malformed/inline-quoted verdict must never read as approval. Each
  was a bug the dogfood caught and closed.

## Post-mortem

(pending)
