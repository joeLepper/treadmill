# model-review-panel — cross-model review panel (ADR-0107 / ADR-0105)

Fan a review artifact (an ADR, a plan, a diff, a design note) out to a panel of
models from distinct provider families, collect an adversarial review from each,
and print ranked verdicts plus a synthesis. We use single-shot calls for the default
legs; optional AR legs each start one bare Netlify Agent Runner. We use the panel
while authoring or gating work.

## Routing

Each family goes to the provider we already pay for, and OpenCode Go budget is spent
ONLY on models we cannot get elsewhere:

| Leg | Route | Auth | Notes |
|---|---|---|---|
| open-weight (Qwen/GLM/Kimi/MiniMax) | fleet gateway (`tools/model-gateway`, LiteLLM -> OpenCode Go) | Go API key (gateway holds it) | the Go budget; one model per family for diversity |
| gpt | `codex exec` in an ISOLATED minimal `CODEX_HOME` (only a fresh copy of `~/.codex/auth.json`) | Codex CLI OAuth (NOT an API key) | minimal home avoids the default home's MCP-server startup (~9s vs 240s+) |
| claude | `claude -p` with `ANTHROPIC_API_KEY` unset | Claude Code subscription (NOT an API key) | unsetting the key forces the claude.ai login |
| gpt-ar | Netlify Agent Runner: codex, gpt-6.1-sol, high effort | Netlify token | counts as gpt |
| gemini-ar | Netlify Agent Runner: gemini, gemini-3.1-pro-preview, high effort | Netlify token | counts as gemini |
| claude-ar | Netlify Agent Runner: claude, runner default model, high effort | Netlify token | counts as claude |

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

## Agent Runner legs

We select AR legs with `--families`, for example `--families gpt-ar,claude`.
The known names are `open-weight`, `gpt`, `claude`, `gpt-ar`, `gemini-ar` and
`claude-ar`. An unknown name exits 2 and lists the known names. Quorum counts
model families, not routes: `gpt,gpt-ar` supplies only one family. `gemini` is
also a valid `--author-family`.

Each AR leg starts in Ask mode (`mode: "ask"`) and gets a read-only brief plus
the same rubric and artifact as the other legs. We read its newest session's final message as the review. The default site
is `fleet-ar-review-testing` in the `netlify-testing` team, billed to that team's
Netlify credits. It has no repository, environment variables or functions.
`PANEL_AR_SITE_ID` overrides the site; an override may not have those properties.
Artifact text goes to Netlify Agent Runners; `gemini-ar` reaches Google through them.

Authentication uses `NETLIFY_AUTH_TOKEN` when set, otherwise the Netlify CLI's
current login (`netlify login`). We never print the token. The timeout budget is
at least 1,200 seconds for each AR leg, even with a lower `--timeout`; a completed
review returns sooner. One observed gpt-ar review of a 335-line diff took about
75–85 seconds, not a guaranteed duration.

## Guarantees

- **Degrades, never crashes.** A leg that errors, times out, or returns no visible
  text is reported (`error` / `timeout` / `no-output` / `no-verdict`) and is NEVER
  counted as an approval. A fully-degraded panel is `no-verdict`, so a gate never
  reads silence as approval.
- **Reasoning is stripped.** Inline `<think>...</think>` (MiniMax) and
  `<reasoning>` blocks — including a dangling open tag from a truncated reply — are
  removed before parsing, so a reasoning model's plan never leaks into the verdict.
- **Credentials follow the route.** gpt and claude use OAuth/subscription; the
  gateway holds the Go API key. AR legs use a Netlify token and spend Netlify credits.
- **CLI tool restrictions.** The artifact is untrusted input inlined in the prompt.
  The gpt and claude CLI legs restrict tools: the codex leg runs `-s read-only -c
  approval_policy=never` (no execute/write/network) in an empty cwd off the token's
  path; the claude leg uses a POSITIVE allowlist that grants NO tools
  (`--allowedTools __panel_no_tools__`). A denylist is fail-open on this boundary —
  it left Agent/Workflow/Skill/ToolSearch exposed, and Agent/Workflow spawn subagents
  that do not inherit it (execution + exfil). `--allowedTools ""` is ignored (Bash
  still ran), so a non-empty sentinel is required.

- **AR legs use Ask mode.** The runner applies a read-only permission profile for
  Codex, deny rules for Claude and Gemini, read-only database access and no deploy.
  The brief also forbids writes, artifact instructions and posts. The sandbox still
  carries platform tokens: a Netlify API token and the AI Gateway key. An empty
  review site does not mean the sandbox has no credentials.
- **AR failures never approve.** `error`, `cancelled`, `timed_out`, `await_input`,
  a runner still active at its deadline, or more than three consecutive status-read
  failures degrade the leg. Missing review text or a missing verdict also cannot
  approve. When a leg gives up on a nonterminal runner, it tries to cancel it. A
  failed cancellation prints a warning with `netlify agents:stop <id>`; the runner
  may still spend credits.

- **Interrupts cancel waiting runners.** On Ctrl-C or SIGTERM, the panel attempts
  to cancel every runner a leg is still waiting on, then exits 130 with
  `panel: interrupted; Agent Runner legs were cancelled`. Cancellation failures
  still print the stop command; the final message does not override those warnings.

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
4. Wiring as a CI gate: pin `--min-quorum-families >= 2` and do NOT expose the flag
   to the gate caller (a caller could set 1 for a lone-reviewer pass).
5. Truncation is caught only on the gateway leg (`finish_reason=length`). The codex
   and claude legs have no truncation check — safe today only because the rubric puts
   the VERDICT line first (truncation cuts findings, not the verdict), not parity.

## As a skill (agents)

`review-panel` is the shared skill wrapping this CLI so any harness (Claude Code and
Codex) invokes it identically (ADR-0103). It adds a one-call helper,
`.claude/skills/review-panel/scripts/review-pr.sh <PR|branch|file>`, that builds the diff
artifact (excluding generated/vendored files) and runs the panel, exiting with the panel's
own fail-closed code — so it drops straight into a plan `validation` gate. See
`.claude/skills/review-panel/SKILL.md`.
