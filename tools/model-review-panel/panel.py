#!/usr/bin/env python3
"""Cross-model review panel (ADR-0107 / ADR-0105).

Fan a review artifact (an ADR, a plan, a diff, a design note) out to a panel of
models spanning THREE provider families, collect an adversarial review from each,
and print ranked verdicts plus a synthesis. One command; single-shot calls, not
heavyweight agentic sessions.

Routing (operator directive 2026-09-12) — each family goes to the provider we
already pay for, and OpenCode Go budget is spent ONLY on open-weight models:
  - open-weight (Qwen/GLM/Kimi/MiniMax) -> the fleet gateway (LiteLLM -> OpenCode Go)
  - gpt        -> `codex exec` on the Codex CLI OAuth session (NOT an API key)
  - claude     -> `claude -p` on the Claude Code subscription (ANTHROPIC_API_KEY
                  is unset so the subscription login is used, NOT the API key)

The panel degrades: a reviewer that errors or returns no visible text is reported
as such and never counted as a silent approval; the panel still returns the rest.

Usage:
  panel.py review --artifact PATH [--models qwen3.8-max,glm-5.3,...]
                  [--families open-weight,gpt,claude]
                  [--format human|json] [--timeout SECONDS]
"""
import argparse
import concurrent.futures as futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

# --- configuration -----------------------------------------------------------

GATEWAY_URL = os.environ.get("PANEL_GATEWAY_URL", "http://127.0.0.1:4250/v1")
GATEWAY_SECRET = os.environ.get(
    "PANEL_GATEWAY_SECRET", os.path.expanduser("~/model-gateway/secret.env")
)
# One model per open-weight FAMILY (genuine cross-family diversity), plus the two
# paid families. Override the open-weight set with --models.
DEFAULT_OPEN_WEIGHT = ["qwen3.8-max", "glm-5.3", "kimi-k3", "minimax-m3"]
DEFAULT_FAMILIES = ["open-weight", "gpt", "claude"]
# A reasoning model burns ~1000 tokens before any visible text (measured on
# glm-5.2/5.3), so the ceiling must clear reasoning AND a full review.
MAX_TOKENS = 4000

RUBRIC = """You are one reviewer on an adversarial cross-model panel. Review the ARTIFACT below.

Rules:
- A finding is a claim you can demonstrate. State the trigger (inputs/state) and the wrong outcome.
- Cite the exact section or line the artifact contradicts. If no invariant is at risk, it is a preference; label it.
- Rank each finding BLOCKING (an invariant an adversary or an ordinary accident can walk through) or NON-BLOCKING.
- Do not pad. Fewer, harder findings beat many speculative ones.

Output EXACTLY this shape and nothing before it:
VERDICT: block | approve-with-notes | approve
1. [BLOCKING|NON-BLOCKING] <one-line claim> - <trigger> - <where in the artifact>
2. ...
(If there are no findings, write "No findings." after the VERDICT line.)
"""

VERDICT_RANK = {"block": 2, "approve-with-notes": 1, "approve": 0}


# --- helpers -----------------------------------------------------------------

def load_gateway_secret(path):
    """Read GATEWAY_MASTER_KEY from the gateway secret.env without echoing it."""
    key = os.environ.get("GATEWAY_MASTER_KEY")
    if key:
        return key
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                m = re.match(r"^(?:export\s+)?GATEWAY_MASTER_KEY=(.+)$", line)
                if m:
                    return m.group(1).strip().strip('"').strip("'")
    except OSError:
        return None
    return None


def strip_reasoning(text):
    """Remove inline reasoning some models emit (e.g. MiniMax <think>...</think>)."""
    if not text:
        return text
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # A dangling open tag (truncated mid-reasoning) means no usable answer.
    text = re.sub(r"<think>.*$", "", text, flags=re.DOTALL | re.IGNORECASE)
    return text.strip()


def parse_verdict(text):
    if not text:
        return None
    m = re.search(r"VERDICT:\s*(block|approve-with-notes|approve)", text, re.IGNORECASE)
    return m.group(1).lower() if m else None


def build_prompt(artifact_path, artifact_text):
    return f"{RUBRIC}\n\nARTIFACT ({artifact_path}):\n\n{artifact_text}\n"


# --- provider legs -----------------------------------------------------------

def call_gateway(model, prompt, timeout, master_key):
    """Open-weight leg: POST to the fleet gateway (LiteLLM -> OpenCode Go)."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": MAX_TOKENS,
    }).encode()
    req = urllib.request.Request(
        f"{GATEWAY_URL}/chat/completions", data=body,
        headers={"Authorization": f"Bearer {master_key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        d = json.load(resp)
    choices = d.get("choices") or [{}]
    return (choices[0].get("message", {}) or {}).get("content") or ""


def call_codex(prompt, timeout, model=None):
    """GPT leg: `codex exec` on the Codex CLI OAuth session. Runs in an ISOLATED
    minimal CODEX_HOME holding only a fresh copy of the OAuth token — the default
    home loads MCP servers (Fran's fran_msg, etc.) whose startup pushed this leg
    past a 240s timeout; a clean home returns in ~9s. `-o` writes the final message
    to a file, so we never parse the event stream."""
    if not shutil.which("codex"):
        raise RuntimeError("codex CLI not found")
    src_auth = os.path.expanduser("~/.codex/auth.json")
    if not os.path.exists(src_auth):
        raise RuntimeError("no Codex OAuth token (~/.codex/auth.json)")
    with tempfile.TemporaryDirectory() as td:
        home = os.path.join(td, "home")
        os.makedirs(home)
        shutil.copy(src_auth, os.path.join(home, "auth.json"))  # fresh token, no MCP
        out = os.path.join(td, "last.txt")
        env = dict(os.environ)
        env["CODEX_HOME"] = home
        cmd = ["codex", "exec", "--skip-git-repo-check",
               "--dangerously-bypass-approvals-and-sandbox", "-o", out]
        if model:
            cmd += ["-m", model]
        cmd += [prompt]
        subprocess.run(cmd, cwd=td, timeout=timeout, capture_output=True, text=True,
                       env=env, stdin=subprocess.DEVNULL)
        try:
            with open(out) as fh:
                return fh.read()
        except OSError:
            return ""


def call_claude(prompt, timeout, model="sonnet"):
    """Claude leg: `claude -p` on the subscription. Unset ANTHROPIC_API_KEY so the
    claude.ai login is used, not a stray API key (operator: subscription, not key)."""
    if not shutil.which("claude"):
        raise RuntimeError("claude CLI not found")
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    proc = subprocess.run(
        ["claude", "-p", prompt, "--model", model],
        env=env, timeout=timeout, capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    # Drop the one-off connector/auth warning lines claude may print to stdout.
    lines = [l for l in proc.stdout.splitlines()
             if not l.strip().startswith(("⚠", "Warning:"))]
    return "\n".join(lines).strip()


# --- reviewer orchestration --------------------------------------------------

def run_reviewer(name, family, fn, timeout):
    start = time.time()
    result = {"name": name, "family": family, "verdict": None,
              "status": "ok", "error": None, "latency_s": None, "review": ""}
    try:
        raw = fn()
        review = strip_reasoning(raw)
        result["review"] = review
        if not review:
            result["status"] = "no-output"  # reasoning consumed the budget, or empty
        else:
            v = parse_verdict(review)
            result["verdict"] = v
            if v is None:
                result["status"] = "no-verdict"
    except futures.TimeoutError:
        result["status"], result["error"] = "timeout", f"timed out after {timeout}s"
    except (subprocess.TimeoutExpired,) as e:
        result["status"], result["error"] = "timeout", str(e)
    except (urllib.error.HTTPError,) as e:
        detail = ""
        try:
            detail = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception:
            pass
        result["status"], result["error"] = "error", f"HTTP {e.code}: {detail or e.reason}"
    except Exception as e:  # noqa: BLE001 - a leg failure must degrade, not crash the panel
        result["status"], result["error"] = "error", str(e)
    result["latency_s"] = round(time.time() - start, 1)
    return result


def build_roster(families, open_weight_models, prompt, timeout, master_key):
    roster = []
    if "open-weight" in families:
        if master_key:
            for m in open_weight_models:
                roster.append((m, "open-weight",
                               lambda m=m: call_gateway(m, prompt, timeout, master_key)))
        else:
            roster.append(("open-weight", "open-weight",
                           lambda: (_ for _ in ()).throw(RuntimeError(
                               "no gateway master key; is the gateway configured?"))))
    if "gpt" in families:
        roster.append(("gpt", "gpt", lambda: call_codex(prompt, timeout)))
    if "claude" in families:
        roster.append(("claude", "claude", lambda: call_claude(prompt, timeout)))
    return roster


def panel_verdict(results):
    seen = [r["verdict"] for r in results if r["verdict"]]
    if not seen:
        return "no-verdict"
    return max(seen, key=lambda v: VERDICT_RANK.get(v, 0))


# --- output ------------------------------------------------------------------

def render_human(artifact, results):
    out = [f"Cross-model review panel — {artifact}",
           f"Reviewers: {len(results)} | Panel verdict: {panel_verdict(results).upper()}", ""]
    order = {"block": 0, "approve-with-notes": 1, "approve": 2, None: 3}
    for r in sorted(results, key=lambda r: order.get(r["verdict"], 3)):
        tag = r["verdict"].upper() if r["verdict"] else r["status"].upper()
        head = f"[{tag}] {r['name']} ({r['family']}, {r['latency_s']}s)"
        out.append(head)
        if r["status"] != "ok":
            out.append(f"    ! {r['status']}: {r['error'] or 'no usable output'}")
        if r["review"]:
            body = "\n".join("    " + l for l in r["review"].splitlines() if l.strip())
            out.append(body)
        out.append("")
    blocking = sum(1 for r in results if r["verdict"] == "block")
    degraded = [r["name"] for r in results if r["status"] != "ok"]
    out.append(f"Synthesis: {blocking} reviewer(s) blocking; "
               f"{sum(1 for r in results if r['verdict']=='approve-with-notes')} with notes; "
               f"{sum(1 for r in results if r['verdict']=='approve')} approve.")
    if degraded:
        out.append(f"Degraded (not counted): {', '.join(degraded)}.")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="Cross-model review panel")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rv = sub.add_parser("review", help="review an artifact with the panel")
    rv.add_argument("--artifact", required=True, help="path to the file to review")
    rv.add_argument("--models", default=",".join(DEFAULT_OPEN_WEIGHT),
                    help="comma-separated open-weight models")
    rv.add_argument("--families", default=",".join(DEFAULT_FAMILIES),
                    help="comma-separated: open-weight,gpt,claude")
    rv.add_argument("--format", choices=["human", "json"], default="human")
    rv.add_argument("--timeout", type=int, default=240)
    args = ap.parse_args()

    try:
        with open(args.artifact) as fh:
            artifact_text = fh.read()
    except OSError as e:
        print(f"cannot read artifact: {e}", file=sys.stderr)
        return 2

    prompt = build_prompt(args.artifact, artifact_text)
    families = [f.strip() for f in args.families.split(",") if f.strip()]
    open_weight_models = [m.strip() for m in args.models.split(",") if m.strip()]
    master_key = load_gateway_secret(GATEWAY_SECRET)
    roster = build_roster(families, open_weight_models, prompt, args.timeout, master_key)

    results = []
    with futures.ThreadPoolExecutor(max_workers=len(roster) or 1) as ex:
        fut = {ex.submit(run_reviewer, n, fam, fn, args.timeout): n for n, fam, fn in roster}
        for f in futures.as_completed(fut):
            results.append(f.result())

    if args.format == "json":
        print(json.dumps({"artifact": args.artifact,
                          "panel_verdict": panel_verdict(results),
                          "reviewers": results}, indent=2))
    else:
        print(render_human(args.artifact, results))
    # Exit non-zero if the panel blocks, so a gate can key on it.
    return 1 if panel_verdict(results) == "block" else 0


if __name__ == "__main__":
    sys.exit(main())
