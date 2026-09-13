# model-review-panel — cross-model review panel (ADR-0107 / ADR-0105)

Fan a review artifact (an ADR, a plan, a diff, a design note) out to a panel of
models spanning THREE provider families, collect an adversarial review from each,
and print ranked verdicts plus a synthesis — one command, single-shot calls (not
heavyweight agentic sessions). This is the ergonomic primitive on top of the fleet
model gateway: any agent can run it while authoring or gating work.

## Routing (operator directive 2026-09-12)

Each family goes to the provider we already pay for, and OpenCode Go budget is spent
ONLY on models we cannot get elsewhere:

| Family | Route | Auth | Notes |
|---|---|---|---|
| open-weight (Qwen/GLM/Kimi/MiniMax) | fleet gateway (`tools/model-gateway`, LiteLLM -> OpenCode Go) | Go API key (gateway holds it) | the Go budget; one model per family for diversity |
| gpt | `codex exec` in an ISOLATED minimal `CODEX_HOME` (only a fresh copy of `~/.codex/auth.json`) | Codex CLI OAuth (NOT an API key) | minimal home avoids the default home's MCP-server startup (~9s vs 240s+) |
| claude | `claude -p` with `ANTHROPIC_API_KEY` unset | Claude Code subscription (NOT an API key) | unsetting the key forces the claude.ai login |

The gateway fronts open-weight ONLY, so gpt-*/grok-* can never be routed there and
no Go budget is spent on them.

## Usage

```
panel.py review --artifact PATH
                [--models qwen3.8-max,glm-5.3,kimi-k3,minimax-m3]   # open-weight set
                [--families open-weight,gpt,claude]
                [--format human|json] [--timeout 240]
```

Exit code is 0 ONLY when the panel does not block, returns a real verdict, AND has
a cross-family quorum (verdicts from >= `--min-quorum-families` distinct families,
default 2). `block`, a fully-degraded `no-verdict`, and an approve that lacks the
quorum (e.g. one lone surviving reviewer) all exit non-zero, so a gate FAILS CLOSED
— silence, total failure, or a one-model pass is never read as a full-panel
approval. Output
lists each reviewer's VERDICT (block > approve-with-notes > approve) and findings,
then a synthesis. The panel verdict is the WORST reviewer verdict.

## Guarantees

- **Degrades, never crashes.** A leg that errors, times out, or returns no visible
  text is reported (`error` / `timeout` / `no-output` / `no-verdict`) and is NEVER
  counted as an approval. A fully-degraded panel is `no-verdict`, so a gate never
  reads silence as approval.
- **Reasoning is stripped.** Inline `<think>...</think>` (MiniMax) and
  `<reasoning>` blocks — including a dangling open tag from a truncated reply — are
  removed before parsing, so a reasoning model's plan never leaks into the verdict.
- **No API keys.** gpt and claude use OAuth/subscription; only the gateway holds the
  Go API key.

## Config

- Gateway URL: `PANEL_GATEWAY_URL` (default `http://127.0.0.1:4250/v1`).
- Gateway master key: `GATEWAY_MASTER_KEY` env, else read from `PANEL_GATEWAY_SECRET`
  (default `~/model-gateway/secret.env`).
- `MAX_TOKENS=4000` — sized to clear a reasoning model's hidden-token cost
  (~1000 tokens before any visible text on glm-5.2/5.3) plus a full review.

## Tests

`python3 panel_test.py` — pure-function + stubbed-leg tests (no network): reasoning
strip, verdict parse, worst-verdict rank, and graceful degradation (error / empty /
no-verdict / all-degraded). The live legs are exercised by running the tool.

## Known gaps

1. Open-weight leg needs the fleet gateway up AND OpenCode Go balance/quota (a
   spent 5-hour cap returns HTTP 401 "Insufficient balance"; the leg degrades).
2. v1 gateway is config-mode (the model_list is the allowlist); per-caller virtual
   keys + budgets (ADR-0107 DB-mode) are a governance follow-up.
3. The gpt leg copies `~/.codex/auth.json` per call; if that token is expired the
   leg degrades (report), it does not refresh it.
