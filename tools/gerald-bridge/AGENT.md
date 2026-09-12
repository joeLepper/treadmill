# gerald-bridge — open-weight Codex sibling (Gerald) on OpenCode Go

Implements ADR-0104: **Gerald**, an open-weight sibling, runs the Codex harness
pointed at the **OpenCode Go** open-weight models through a local Responses→Chat
shim, and joins the fleet messaging bus as a bidirectional peer. This directory is
the **source of record**; the live runtime deploys under `~/gerald/` (runtime data
— the shim venv, the dedup ledger, the outbox spool, the API key — stays out of
the repo).

Gerald reuses the Fran substrate (ADR-0102, `tools/fran-bridge/`) almost verbatim.
He differs in three ways: an **open-weight model provider**, a **Responses→Chat
shim**, and an **open-weight-only restriction**.

## Why the shim exists (the load-bearing constraint)
Codex 0.154 speaks the OpenAI **Responses** API only to custom providers (it
dropped `wire_api = "chat"`). OpenCode Go's open-weight models speak **Chat
Completions** only (`/responses` returns "not supported for format openai"). They
do not meet directly. The shim (LiteLLM, `use_chat_completions_api`) bridges
Responses→Chat and injects the required `x-opencode-session` routing header.

## Layout
| Path | Role |
|---|---|
| `bridge/inbound.mjs` | peer→Gerald receiver. Durable dedup ledger, `codex queue` into Gerald's thread, commit on `task_complete`. Supports a per-turn `--model` override. |
| `bridge/deliver-inbound.mjs` | wrapper: parses a `[[model: <name>]]` marker (open-weight allowlist), strips it, derives the dedupKey, hands to `inbound.mjs`. |
| `bridge/outbound-next.mjs` | Gerald→peer. `peek` / `ack` the outbox spool. |
| `bridge/msg-server.mjs` | Gerald's outbound MCP server (`gerald_msg`): `send_message`, `list_peers`. `from: "gerald"`. |
| `bridge/inbound.test.mjs`, `bridge/test.mjs` | tests (11 inbound + msg-server). |
| `bridge-session/CLAUDE.md` | the `gxbridge` relay session spec. |
| `bridge-session/gerald-bridge-{supervise,reap}.sh` | supervise/reap the `gxbridge` relay (named `gxbridge`, NOT `gerald-bridge`, to avoid a tmux prefix-collision with `gerald`). |
| `session/gerald-{supervise,reap}.sh` | supervise/reap Gerald's `gerald` Codex session (starts the shared daemon with the Go key; launches `codex --profile gerald`). |
| `session/auth-guard.sh` | ExecStartPre: Go key present, shim serving (waits for it), profile pins `opencode_go` via the shim, and **open-weight-only denylist** (refuses a shim config naming any gpt/claude/grok/… model). |
| `session/AGENTS.md` | Gerald's Codex operating brief (reviewer role, `send_message` reply protocol, open-weight-only rule). |
| `session/secret.env.example` | template for `~/gerald/secret.env` (the Go key; never committed). |
| `shim/config.yaml` | LiteLLM model list — **open-weight models only**. |
| `shim/gerald-shim-supervise.sh` | shim launcher (validates YAML, requires the key). |
| `shim/requirements.txt` | pinned `litellm[proxy]` (recreate the venv from this). |
| `codex/isolated-home-config.toml` | the config for Gerald's ISOLATED Codex home (`~/gerald/.codex/config.toml`): default `opencode_go` provider + model, the `gerald_msg` MCP, trusted project. No `auth.json`, no proprietary provider. No secrets. |
| `systemd/gerald{,-shim,-bridge}.service` | the three user units. |
| `peers.json` | outbound destination allowlist. |

## Deploy
Copy the changed file to its `~/gerald/` counterpart and act on the owner unit.
Runtime state (`~/gerald/shim/venv`, `~/gerald/bridge/ledger`, `~/gerald/outbox`,
`~/gerald/.session-uuid`, `~/gerald/secret.env`) is machine-local — never copy it
back into the repo.

| Changed | Copy to | Action |
|---|---|---|
| `shim/config.yaml`, `shim/gerald-shim-supervise.sh` | `~/gerald/shim/` | `systemctl --user restart gerald-shim.service` |
| `bridge/inbound.mjs`, `deliver-inbound.mjs`, `outbound-next.mjs` | `~/gerald/bridge/` | none — re-read per drain |
| `bridge/msg-server.mjs` | `~/gerald/bridge/` | `systemctl --user restart gerald.service` (Codex spawns it) |
| `session/*` (supervise/reap/auth-guard/AGENTS.md) | `~/gerald/` | `systemctl --user restart gerald.service` |
| `bridge-session/*` | `~/gerald/bridge-session/` | `systemctl --user restart gerald-bridge.service` |
| `codex/isolated-home-config.toml` | `~/gerald/.codex/config.toml` | `systemctl --user restart gerald.service` |
| `systemd/*.service` | `~/.config/systemd/user/` | `systemctl --user daemon-reload && systemctl --user restart <unit>` |

First-time setup: create `~/gerald/secret.env` from the example (chmod 600);
`python3 -m venv ~/gerald/shim/venv && ~/gerald/shim/venv/bin/pip install -r
shim/requirements.txt`; install `codex/isolated-home-config.toml` to
`~/gerald/.codex/config.toml` and symlink `~/gerald/.codex/packages/standalone` →
`~/.codex/packages/standalone` (so the isolated daemon finds the managed binary).
Gerald's home must have NO `auth.json`. He runs his OWN daemon under
`CODEX_HOME=~/gerald/.codex` — nothing to reconcile with Fran's daemon.

## Contract
- **Open-weight only — enforced on the configured route (ADR-0104).** Gerald's
  isolated home has no `auth.json` and only the `opencode_go` provider, so the
  normal request routes cannot select a proprietary model — verified by foil
  (`--model gpt-6-astra` errors; `-c model_provider=openai` is refused). Defense
  in depth: the shim lists open-weight models only; the auth-guard parses the shim
  config against an open-weight ALLOWLIST (fail-closed), pins `opencode_go` to the
  local shim over `wire_api=responses`, and refuses to start if the home has
  `auth.json` or a non-`opencode_go` provider; the unit unsets
  `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`; AGENTS.md forbids proprietary use.
  **Not a hard sandbox:** Gerald runs as the same UNIX user as the fleet with a
  bypassed shell, so a policy-breaking Gerald could read `~/.codex/auth.json` or
  set `CODEX_HOME=~/.codex` — as any fleet agent could reach any other's files.
  The guarantee is configured-route isolation + cooperative policy; a hard
  boundary (separate UID/container) is a fleet-wide follow-up.
- **Inbound / outbound** semantics match `tools/fran-bridge/` (exactly-once COMMIT
  as a ledger capability; the shipped bridge runs fire-and-forget `deliver`;
  outbound is at-least-once, duplicate possible). See that AGENT.md's Contract.
- **Per-message model.** A `[[model: <name>]]` marker (open-weight allowlist:
  `qwen3.8-max`, `kimi-k2.7-code`, `glm-5.3`, `minimax-m3`) makes the bridge set
  `codex queue --model` for that turn. Default is `qwen3.8-max`.

## Known gaps
1. Bridge/session code is duplicated from `tools/fran-bridge/` (ADR-0102/0104
   follow-up: one canonical copy).
2. Codex lacks registry metadata for these custom models (context window/pricing
   unknown) — set the context window in config to avoid mismanagement.
3. `kimi-k2.7-code` / `glm-5.3` sometimes emit empty output; `qwen3.8-max` is the
   reliable default. Model quality varies; per-model Go dollar caps can throttle.
4. Same deploy-divergence and bus-name-instability caveats as `tools/fran-bridge/`.
