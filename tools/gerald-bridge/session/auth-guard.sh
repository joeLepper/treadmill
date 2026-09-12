#!/usr/bin/env bash
# ADR-0104 auth guard: Gerald must run open-weight-only, in his ISOLATED Codex
# home, through the local shim. Fails loud (blocks unit start) on any violation.
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
GHOME="$HOME/gerald"
CODEX_HOME="$GHOME/.codex"
SHIM_PY="$GHOME/shim/venv/bin/python"
fail(){ echo "GERALD AUTH GUARD FAIL: $*" >&2; exit 1; }

# 1. Go key present and not a placeholder.
set -a; . "$GHOME/secret.env" 2>/dev/null || fail "secret.env missing"; set +a
case "${OPENCODE_API_KEY:-}" in ""|*REPLACE_WITH_GO_KEY*) fail "OPENCODE_API_KEY missing/placeholder";; esac

# 2. Shim reachable and healthy (LiteLLM boots ~15s; -f fails on non-2xx — B4).
ok=0; for _ in $(seq 1 30); do
  curl -fsS -o /dev/null --max-time 3 http://127.0.0.1:4141/health/liveliness && { ok=1; break; }; sleep 2
done
[ "$ok" = 1 ] || fail "shim not healthy on 127.0.0.1:4141 after 60s"

# 3. STRUCTURAL open-weight isolation (ADR-0104, reviews B1). Gerald's isolated
# home must have NO ChatGPT auth and define NO proprietary provider, so there is
# nothing proprietary to select even with a runtime provider override.
[ -e "$CODEX_HOME/auth.json" ] && fail "Gerald's home has auth.json — ChatGPT auth must not exist in his isolated home"

# 4. Validate the isolated config + shim allowlist by PARSING (not grep). The shim
# model list must be a SUBSET of the approved open-weight allowlist (fail-closed —
# fixes the denylist fail-open where e.g. anthropic/claude slipped past an openai/
# grep). Gerald's config must define only opencode_go and default to it.
"$SHIM_PY" - "$GHOME/shim/config.yaml" "$CODEX_HOME/config.toml" <<'PY' || exit 1
import sys, yaml, tomllib
APPROVED = {"qwen3.8-max", "kimi-k2.7-code", "glm-5.3", "minimax-m3"}
shim_path, cfg_path = sys.argv[1], sys.argv[2]
def die(m): print("GERALD AUTH GUARD FAIL:", m, file=sys.stderr); sys.exit(1)
y = yaml.safe_load(open(shim_path))
for m in (y.get("model_list") or []):
    name = m.get("model_name", "")
    upstream = (m.get("litellm_params") or {}).get("model", "")
    # strip a leading provider tag (e.g. "openai/") — validate the model NAME.
    umodel = upstream.split("/", 1)[1] if "/" in upstream else upstream
    if name not in APPROVED or umodel not in APPROVED:
        die(f"shim exposes non-allowlisted model: name={name!r} upstream={upstream!r} (open-weight allowlist only)")
c = tomllib.load(open(cfg_path, "rb"))
provs = set(c.get("model_providers", {}))
if provs - {"opencode_go"}:
    die(f"Gerald's config defines non-opencode_go provider(s): {provs - {'opencode_go'}}")
if c.get("model_provider") != "opencode_go":
    die(f"Gerald's default model_provider is not opencode_go: {c.get('model_provider')!r}")
if c.get("model") not in APPROVED:
    die(f"Gerald's default model is not open-weight: {c.get('model')!r}")
# opencode_go MUST route through the local shim over the Responses wire, or the
# open-weight guard is bypassed by pointing the provider elsewhere (review: a
# base_url=https://example.invalid/v1 passed before this check was restored).
og = c.get("model_providers", {}).get("opencode_go", {})
if og.get("base_url") != "http://127.0.0.1:4141/v1":
    die(f"opencode_go base_url is not the local shim: {og.get('base_url')!r}")
if og.get("wire_api") != "responses":
    die(f"opencode_go wire_api is not responses: {og.get('wire_api')!r}")
if og.get("env_key") != "OPENCODE_API_KEY":
    die(f"opencode_go env_key is not OPENCODE_API_KEY: {og.get('env_key')!r}")
print("gerald auth guard OK: Go key, shim healthy, isolated home (no ChatGPT), opencode_go->shim pinned, open-weight allowlist enforced")
PY
exit 0
