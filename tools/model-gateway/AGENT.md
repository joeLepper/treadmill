# model-gateway — fleet model gateway (ADR-0107)

The routing + policy plane for open-weight models. LiteLLM fronts OpenCode Go's
open-weight catalog behind ONE OpenAI-compatible endpoint, so any fleet caller (the
`model-review-panel`, a sibling subagent) reaches Qwen/GLM/Kimi/MiniMax uniformly
without holding the Go key. GPT and Claude do NOT go through here — they route via
their own OAuth CLIs (see `tools/model-review-panel`), so the Go budget is spent
ONLY on models we cannot get elsewhere.

## v1 is config-mode (deliberate)

The `model_list` in `config.yaml` IS the allowlist: only open-weight models are
listed, so a request for a proprietary model (`gpt-*`, `grok-*`) returns HTTP 400
"Invalid model name" and is never routed. No router fallbacks are configured, so
there is no fallback path to an out-of-list model (ADR-0107 Gerald finding:
direct-deny must also be fallback-deny — here there is simply no fallback).

Per-caller virtual keys + budgets (the ADR-0107 DB-mode spike: Postgres + prisma,
403 allowlist / 401 management / 429 budget) are a GOVERNANCE FOLLOW-UP, deferred
until a second caller needs distinct policy. v1 avoids a persistent Postgres failure
surface for a single caller; OpenCode Go's own per-model dollar caps bound spend.

## Layout

| Path | Role |
|---|---|
| `config.yaml` | LiteLLM model_list = the open-weight allowlist; `master_key` from env |
| `gateway-supervise.sh` | ExecStart: source secret.env, run litellm from the OWN venv on `$GATEWAY_PORT` (4250) |
| `systemd/model-gateway.service` | the user unit (Restart=on-failure) |
| `requirements.txt` | pinned `litellm[proxy]` (recreate the venv from this) |
| `secret.env.example` | template for `~/model-gateway/secret.env` (master key + Go key; chmod 600, never committed) |

## Deploy

Runtime lives at `~/model-gateway/` (its OWN venv — no dependency on any sibling).

```
cp config.yaml gateway-supervise.sh ~/model-gateway/
cp systemd/model-gateway.service ~/.config/systemd/user/
python3 -m venv ~/model-gateway/venv && ~/model-gateway/venv/bin/pip install -r requirements.txt
# create ~/model-gateway/secret.env from the example (chmod 600)
systemctl --user daemon-reload && systemctl --user enable --now model-gateway.service
```

## Verify

```
curl -s localhost:4250/v1/models -H "Authorization: Bearer $GATEWAY_MASTER_KEY"    # 6 open-weight, no proprietary
curl -s localhost:4250/v1/chat/completions -H "Authorization: Bearer $GATEWAY_MASTER_KEY" \
  -H 'Content-Type: application/json' -d '{"model":"gpt-5.6-luna","messages":[{"role":"user","content":"hi"}]}'  # HTTP 400 refused
```

## Known gaps

1. Config-mode: no per-caller policy or budget enforcement (see above). Go's own
   caps are the only spend bound.
2. Open-weight calls fail HTTP 401 "Insufficient balance" when the OpenCode Go
   quota/5-hour cap is spent — a billing state, not a gateway fault.
3. Single master key: any holder reaches the whole open-weight allowlist. Per-caller
   keys are the DB-mode follow-up.
