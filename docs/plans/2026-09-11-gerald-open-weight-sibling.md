# Plan: Gerald — open-weight sibling (Codex harness on OpenCode Go)

- **Status:** completed
- **Date:** 2026-09-11
- **Related ADRs:** ADR-0104, ADR-0102, ADR-0103

## Goal

Stand up Gerald: a persistent, autonomous fleet sibling that runs the Codex
harness pointed at the OpenCode Go endpoint, so it codes with open-weight models
while keeping a watchable TUI and the proven `codex queue` injection channel.
Reuse the Fran substrate; add only what differs (provider auth + model pin).

## Success criteria

Each is observable and foil-able:

1. `gerald.service` brings up a Codex TUI in tmux (`gerald` session) that survives
   reboot and restarts on failure.
2. Gerald's model turns are served by `opencode_go` ONLY — verified by a model id
   in the Go catalog and NO `OPENAI_API_KEY`-authenticated call (ADR-0104
   falsifier). Foil: unset the provider pin → the turn must FAIL or bill OpenAI,
   proving the pin is load-bearing.
3. A peer `send`s Gerald via the bridge; the message lands as its own `codex
   queue` turn WITHOUT clobbering Gerald's composer; Gerald replies through the
   outbox and the peer receives `[from Gerald] ...`.
4. The shared app-server daemon carries `OPENCODE_API_KEY` and NO
   `OPENAI_API_KEY`; Fran's sessions still work (no flap).
5. `--dangerously-bypass-approvals-and-sandbox` gives fleet-parity autonomy; a
   full tool-using turn (edit + shell) completes unattended.
6. Gerald appears on cc-dashboard.

## Constraints / scope

### In scope
- `~/gerald` substrate (supervise/reap/auth-guard, config profile, bridge copy).
- `gerald.service` + shared-daemon env reconciliation.
- Repo home `tools/gerald-bridge/` (Gerald-specific config/session/systemd).
- cc-dashboard + registry `from=gerald` label.

### Out of scope
- A headless OpenCode-native sibling (ADR-0104 follow-up).
- De-duplicating the Fran/Gerald bridge code (follow-up; v1 copies).
- Telegram bridge for Gerald.

### Budget
One focused build. The OpenCode Go API key is an external dependency (operator);
all non-model work proceeds without it and the live checks (criteria 2, 5) run
when the key lands.

## Sequence of work

1. **Codex provider profile.** Add `[model_providers.opencode_go]` and
   `[profiles.gerald]` to `~/.codex/config.toml`; pick the default Go model.
2. **Shared-daemon auth.** A gitignored `~/gerald/secret.env` holds
   `OPENCODE_API_KEY`; both supervise scripts start the daemon with it (and
   `-u OPENAI_API_KEY`). Depends on 1.
3. **Session substrate.** `gerald-supervise.sh` / `gerald-reap.sh` (adapt Fran's);
   `auth-guard.sh` verifies `OPENCODE_API_KEY` present + daemon clean + provider
   pinned. Depends on 1-2.
4. **Bridge.** Copy the Fran bridge (`msg-server.mjs`, inbound/outbound) into
   `~/gerald/bridge`; a `gerald-bridge` relay session (or reuse the cxbridge
   pattern). Depends on 3.
5. **systemd + dashboard.** `gerald.service`; add `gerald` to cc-dashboard LABELS.
   Depends on 3-4.
6. **Repo + review.** Land `tools/gerald-bridge/` with an AGENT.md; sibling +
   cross-model (Fran) review; merge per operator authorization.

## Diagram

See ADR-0104.

## Risks / unknowns

- **Shared-daemon env coupling** (criterion 4): starting order between Fran and
  Gerald must not flap the daemon. Mitigation: idempotent env reconcile, never a
  blind restart. Abort-if: bringing Gerald up flaps Fran's session.
- **wire_api mismatch**: Go auto-detects Responses for GPT, Chat for others; for
  open-weight models use `wire_api = "chat"`. Verify against a real turn.
- **Cap throttling**: a model 402/429 mid-task. Gerald must surface it.

## Decisions captured during execution

- (ADR-0104) OpenCode's native TUI ruled out by the render bug; Codex+Go chosen.
- **Wire-format incompatibility (spike):** Codex 0.154 speaks Responses only; Go's
  open-weight models are Chat-only. Resolved with a LiteLLM Responses→Chat shim
  (`gerald-shim.service`, `use_chat_completions_api`, injects `x-opencode-session`).
- **Per-message model** is bridge-driven: `[[model: <name>]]` → `codex queue
  --model` (deterministic), not Gerald self-switching. Proven with `kimi-k2.7-code`.
- **Open-weight enforcement** at three layers: shim allowlist, auth-guard denylist
  (refuses proprietary families), and AGENTS.md. The Go catalog serves closed
  models too, so the guard is load-bearing.
- **Daemon readiness:** the shim is systemd-active before it serves (~15s), so the
  auth-guard WAITS for liveliness; the shim validates its YAML before start to
  avoid a crash-loop. Both fixed after live flaps.
- **Model quality varies:** `qwen3.8-max` is the reliable default; `kimi-k2.7-code`
  and `glm-5.3` sometimes emit empty output. Codex lacks registry metadata for
  these custom models (context-window follow-up).
- **Fleet model gateway (deferred):** generalize the shim to a multi-provider,
  per-caller-policy gateway so any sibling can route to any provider — separate
  ADR + spike after Gerald lands.
- **Open-weight enforcement is STRUCTURAL, via daemon isolation (review-driven).**
  Both cross-model reviews (Fran + Ernie) blocked: the first design shared Fran's
  daemon, whose live ChatGPT auth made the boundary conventional. Fix: Gerald runs
  his own `CODEX_HOME=~/gerald/.codex` daemon (managed binary symlinked) with no
  `auth.json` and only `opencode_go`. Foils confirm proprietary models are
  unreachable. This also removed the shared-daemon coupling and the Fran flap.
- **Review round found disjoint real defects** (evidence for ADR-0105's two-pass
  rule): fail-open denylist (→ allowlist parse), API key in tmux scrollback (→
  in-pane source), cross-thread bootstrap (→ set-difference), shim readiness race
  (→ curl -f wait + exit-78/RestartPreventExitStatus), supervisor exit-0 on
  session loss (→ exit 1).
- **Review-routing + two-cross-model-pass requirement split to ADR-0105** (one
  decision per ADR).

## Post-mortem

- **What worked.** Reusing the Fran/ADR-0102 substrate got a working bus peer
  fast. The spike-first approach caught the two make-or-break unknowns early: the
  Responses/Chat wire-format incompatibility (solved with a LiteLLM shim) and,
  later, that the shared daemon made open-weight enforcement conventional.
- **What surprised us.** Codex 0.154 dropped `wire_api = "chat"`, so an
  OpenAI-compatible endpoint was not enough — a translation shim was required.
  And "open-weight only" turned out to need a dedicated daemon with no ChatGPT
  auth; a profile/provider default was not enforcement.
- **Two cross-model passes earned their keep.** Across two rounds, Fran and Ernie
  found disjoint real defects — a fail-open denylist, an API key leaking into
  tmux, the enforcement gap, and an auth-guard regression introduced mid-fix.
  None would have shipped. This is the evidence behind ADR-0105.
- **Follow-ups (see ADR-0104):** de-dup Fran/Gerald bridge code; runtime
  open-weight detector; a hard (separate-UID) isolation boundary fleet-wide;
  queue-error retryability that separates confirmed rejection from uncertain
  acceptance; the deferred fleet model gateway.
