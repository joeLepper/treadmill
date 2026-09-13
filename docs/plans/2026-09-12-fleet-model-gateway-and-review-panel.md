# Plan: Fleet model gateway + cross-model review panel

- **Status:** drafting
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

1. A persistent gateway exposes ONE OpenAI-compatible endpoint. A request for an
   allowlisted open-weight model succeeds; a request for a non-allowlisted or
   proprietary model (e.g. `gpt-5.6-luna`, `grok-4.6`) is refused (HTTP 4xx).
2. `panel review --artifact <path>` returns >= 3 INDEPENDENT cross-family verdicts
   (from Qwen, Zhipu/GLM, Moonshot/Kimi, MiniMax) each with a VERDICT line and
   findings, in a single invocation, plus a ranked digest.
3. Reasoning is not leaked: MiniMax `<think>...</think>` blocks and provider
   `reasoning_content` are stripped from panel output; an empty-on-budget model is
   reported as `no-output`, never as a silent empty approval.
4. Per-caller policy is real: the panel's virtual key can reach only its allowlist;
   a budget is enforced (with Go price mappings) OR the limitation is stated.
5. Fallback closure: `enforce_fallback_model_access: true` is set and a
   fallback-routed out-of-allowlist request is refused (Gerald's spike-scope
   correction — direct-deny != fallback-deny).
6. Both components ship on a branch with the ADR-0105 two-pass review (Gerald
   open-weight cross-model + a sibling co-sign) before merge.

## Constraints / scope

### In scope
- LiteLLM gateway (DB-mode) fronting OpenCode Go's catalog, as a persistent user
  service, with a master key and per-caller virtual keys.
- Go per-model price mappings; `enforce_fallback_model_access`; api_base pinned to
  the Go endpoint; provider-tag validation.
- A `panel` CLI: fan-out to a cross-family open-weight set, strip reasoning, parse
  verdicts, rank, emit JSON + a human digest.

### Out of scope
- Proprietary models in the panel (gpt/grok flaky; deepseek-v4-pro region-gated).
  The requesting agent is Claude, so the panel's job is NON-Claude open-weight
  breadth.
- Heavyweight agentic per-model subagents (Gerald/Fran remain the deep reviewers).
- Wiring the panel into the ADR/plan/gate skills as a hard gate (Phase 3, later).
- A hard security sandbox (same-UID blast radius stands, per ADR-0107).

### Budget
This session. If Phase 1 cannot be made durable and safe within it, stop and write
a post-mortem rather than ship a fragile service.

## Sequence of work

1. **Gateway service** — Postgres (durable) + LiteLLM DB-mode config fronting Go;
   master key in a chmod-600 secret; systemd unit + supervise; Go price mappings;
   `enforce_fallback_model_access: true`; no cross-family fallbacks configured.
2. **Policy** — mint a `panel` virtual key (open-weight cross-family allowlist +
   budget); foil: allowlisted model 200, proprietary 4xx, fallback-routed 4xx.
3. **Panel tool** — `tools/model-gateway/panel.py`: concurrent fan-out via the
   gateway, `<think>`/reasoning strip, verdict parse, ranked digest, JSON + human
   output; unit tests with a stub gateway.
4. **Review + deploy** — two-pass (Gerald + sibling), deploy the service, demo the
   panel on a real artifact (this plan).

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

(empty)

## Post-mortem

(pending)
