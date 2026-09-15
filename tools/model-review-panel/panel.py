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
# The open-weight models the gateway actually serves. --models is validated against
# this so a paid-family or unknown model id can never be labelled open-weight and
# routed to the gateway (the gateway also 404s it, but fail early and clearly).
OPEN_WEIGHT_ALLOWED = {"qwen3.8-max", "glm-5.2", "glm-5.3",
                       "kimi-k2.7-code", "kimi-k3", "minimax-m3"}
DEFAULT_FAMILIES = ["open-weight", "gpt", "claude"]
# A PASS (exit 0) requires verdicts from at least this many distinct families, so a
# gate never passes on one surviving reviewer as if the full cross-family panel ran.
MIN_QUORUM_FAMILIES = 2
# A reasoning model burns ~1000+ tokens before any visible text (measured on
# glm-5.2/5.3) and a full adversarial review adds more; 4000 truncated glm-5.3
# (finish_reason=length), so give the ceiling headroom for reasoning AND the review.
MAX_TOKENS = 8000

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
    # A dangling open tag (truncated mid-reasoning) means no usable answer — strip to
    # end so a partial "<reasoning>VERDICT: approve" cannot parse as a real verdict.
    text = re.sub(r"<think>.*$", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<reasoning>.*$", "", text, flags=re.DOTALL | re.IGNORECASE)
    return text.strip()


def parse_verdict(text):
    if not text:
        return None
    # Anchor to LINE START (the rubric requires the verdict as its own line), so a
    # quoted "VERDICT: approve" inside prose is ignored. (?![\w-]) rejects "approved"
    # / "approve-with-notes-pending". Return the WORST of all line-start matches
    # (fail-closed): a reviewer that blocks anywhere blocks.
    matches = re.findall(r"^\s*VERDICT:\s*(block|approve-with-notes|approve)(?![\w-])",
                         text, re.IGNORECASE | re.MULTILINE)
    if not matches:
        return None
    return max((m.lower() for m in matches), key=lambda v: VERDICT_RANK.get(v, 0))


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
    ch = (d.get("choices") or [{}])[0]
    if ch.get("finish_reason") == "length":
        # A truncated review is untrustworthy (its verdict/findings may be cut off);
        # degrade rather than count a partial "VERDICT: approve" — raise MAX_TOKENS.
        raise RuntimeError("gateway response truncated (finish_reason=length)")
    return (ch.get("message", {}) or {}).get("content") or ""


def _codex_fatal_line(stderr):
    """Pick the line that names WHY codex exec failed, skipping noise that hides it.

    codex prints, in order: a non-fatal 'Refusing to create helper binaries under
    temporary dir' WARNING (it proceeds), repeated 'Reconnecting... N/5' retries,
    and only THEN the real cause ('401 Unauthorized', 'no credits remaining',
    'stream disconnected before completion'). Returning the first 200 chars shows
    only the warning and masks the cause. We prefer a line naming a known fatal
    signal; else the last non-noise line; else a trimmed head as a last resort."""
    lines = [l.strip() for l in (stderr or "").splitlines() if l.strip()]
    if not lines:
        return ""
    signals = ("no credits remaining", "401 unauthorized", "unauthorized",
               "stream disconnected", "insufficient", "quota", "invalid api key",
               "incorrect api key", "forbidden", "rate limit")
    noise = ("refusing to create helper binaries", "reconnecting...",
             "falling back from websockets")
    for l in reversed(lines):  # the fatal cause is emitted last
        if any(s in l.lower() for s in signals):
            return l[:200]
    for l in reversed(lines):
        if not any(n in l.lower() for n in noise):
            return l[:200]
    return lines[-1][:200]


def call_codex(prompt, timeout, model=None):
    """GPT leg: `codex exec` on the Codex CLI OAuth session. Runs in an ISOLATED
    minimal CODEX_HOME holding only a fresh copy of the OAuth token — the default
    home loads MCP servers (Fran's fran_msg, etc.) whose startup pushed this leg
    past a 240s timeout; a clean home returns in ~9s. `-o` writes the final message
    to a file, so we never parse the event stream.

    SECURITY: the artifact is UNTRUSTED, so we run `-s read-only -c
    approval_policy=never` — never `--dangerously-bypass-approvals-and-sandbox`. A
    prompt-injection payload in the artifact can then not write, execute, or reach
    the network from the review; model-generated commands are auto-denied, not run."""
    if not shutil.which("codex"):
        raise RuntimeError("codex CLI not found")
    src_auth = os.path.expanduser("~/.codex/auth.json")
    if not os.path.exists(src_auth):
        raise RuntimeError("no Codex OAuth token (~/.codex/auth.json)")
    with tempfile.TemporaryDirectory() as td:
        home = os.path.join(td, "home")
        work = os.path.join(td, "work")  # cwd, kept EMPTY and off the token's path
        os.makedirs(home)
        os.makedirs(work)
        shutil.copy(src_auth, os.path.join(home, "auth.json"))  # fresh token, no MCP
        out = os.path.join(work, "last.txt")
        env = dict(os.environ)
        # The ChatGPT OAuth session is the free budget; a stray OPENAI_API_KEY in the
        # env OVERRIDES it and falls back to the (no-credit) API key — the same override
        # that forces Fran to run with `env -u OPENAI_API_KEY`. Unset it so this leg uses
        # the Codex OAuth token, mirroring the claude leg's ANTHROPIC_API_KEY pop.
        env.pop("OPENAI_API_KEY", None)
        env["CODEX_HOME"] = home
        # PREFLIGHT: fail with an ACTIONABLE message if the copied auth is not a
        # ChatGPT OAuth session. A `codex login --with-api-key` anywhere on the box
        # flips the SHARED ~/.codex to api-key mode (auth.json = a bare
        # OPENAI_API_KEY, config forced_login_method="api"); the API key has no
        # credits, so `codex exec` fails deep in a 401/"no credits" retry loop that
        # reads as an infra bug. Detecting it here (mirrors ADR-0102's auth-guard for
        # Fran) turns a fleet-credential incident into one clear line. (2026-09-15.)
        st = subprocess.run(["codex", "login", "status"], env=env,
                            capture_output=True, text=True, timeout=30)
        if "chatgpt" not in (st.stdout + st.stderr).lower():
            # Report the actual status line ("Logged in using an API key"), not the
            # helper-binary warning codex also prints to stderr.
            status = next((l.strip() for l in (st.stdout + "\n" + st.stderr).splitlines()
                          if "logged in" in l.lower() or "not signed" in l.lower()),
                         "unknown")
            raise RuntimeError(
                "codex leg not on ChatGPT OAuth — the free-budget session is gone "
                "(got: " + status[:80] + "). A `codex login --with-api-key` clobbered "
                "the shared ~/.codex. Fix: `codex login` (browser) to restore ChatGPT auth.")
        cmd = ["codex", "exec", "--skip-git-repo-check",
               "-s", "read-only", "-c", "approval_policy=never", "-o", out]
        if model:
            cmd += ["-m", model]
        cmd += ["-"]  # read the prompt from stdin: a large artifact inlined in argv blows ARG_MAX
        # cwd=work (empty, does not contain CODEX_HOME) so a crafted artifact cannot
        # surface the OAuth token from the working directory.
        proc = subprocess.run(cmd, cwd=work, timeout=timeout, capture_output=True, text=True,
                              env=env, input=prompt)
        if proc.returncode != 0:
            # A failed run's output is untrustworthy; degrade rather than risk
            # counting a partial "VERDICT: approve" as a real verdict.
            # Surface the MEANINGFUL error, not the leading line. codex prints a
            # non-fatal "Refusing to create helper binaries under temporary dir"
            # WARNING (it proceeds past it) BEFORE the real fatal cause (401, "no
            # credits remaining", "stream disconnected"). Naive stderr[:200] shows
            # only that warning and masks the true failure (this misled a whole
            # gate diagnosis, 2026-09-15). Prefer lines that name the fatal cause.
            raise RuntimeError(f"codex exec exited {proc.returncode}: "
                               f"{_codex_fatal_line(proc.stderr) or 'no stderr'}")
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
    # SECURITY: the artifact is UNTRUSTED and inlined in the prompt. A DENYLIST is
    # fail-open on this boundary — it left Agent/Workflow/Skill/ToolSearch/Read
    # exposed, and Agent/Workflow spawn subagents that do NOT inherit the denylist
    # (execution + exfil). Use a positive ALLOWLIST that grants NOTHING: a non-empty
    # allowlist of a single nonexistent tool is honoured and denies every real tool
    # (verified: Bash refused, Read denied). The review needs no tools — the artifact
    # is already in the prompt. (`--allowedTools ""` is IGNORED, so Bash still ran;
    # the sentinel name is required.)
    # The prompt (with the large artifact) goes on stdin, not argv: `claude -p` with no positional
    # prompt reads it from stdin, and argv has a length cap a big diff exceeds (ARG_MAX).
    proc = subprocess.run(
        ["claude", "-p", "--model", model, "--allowedTools", "__panel_no_tools__"],
        env=env, timeout=timeout, capture_output=True, text=True, input=prompt,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p exited {proc.returncode}: "
                           f"{(proc.stderr or '').strip()[:200] or 'no stderr'}")
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
    except subprocess.TimeoutExpired as e:
        result["status"], result["error"] = "timeout", str(e)
    except TimeoutError as e:  # socket timeout from the gateway leg
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


def vendor_of(model):
    """The model's VENDOR family, for cross-family quorum — the open-weight models
    span distinct vendors, so they must not collapse to one "open-weight" family."""
    m = model.lower()
    for pref in ("qwen", "glm", "kimi", "minimax", "deepseek", "longcat", "mimo"):
        if m.startswith(pref):
            return pref
    return model


def build_roster(families, open_weight_models, prompt, timeout, master_key):
    roster = []
    if "open-weight" in families:
        if master_key:
            for m in open_weight_models:
                roster.append((m, vendor_of(m),
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


# Every family label the panel can emit (vendor_of outputs + the two paid legs). A
# --author-family outside this set would exclude nothing and fail OPEN, so it is
# rejected up front (validate_coverage_args).
KNOWN_FAMILIES = {"qwen", "glm", "kimi", "minimax", "deepseek", "longcat", "mimo",
                  "gpt", "claude"}


def cross_model_families(results, author_family):
    """Distinct families that returned a verdict, EXCLUDING the author's own family —
    the genuinely-independent voices. On a Go cap the open-weight tier drops out, so
    this is the honest count of cross-model coverage (not "did the panel run").
    Case/whitespace-normalized so a mis-cased --author-family cannot fail open."""
    af = (author_family or "").lower().strip()
    return {r["family"] for r in results
            if r["verdict"] and (not af or r["family"].lower().strip() != af)}


def validate_coverage_args(author_family, min_cross_model):
    """Fail-closed guard on the coverage flags: a misconfiguration must ERROR, never
    silently pass. Returns an error string, or None when the args are safe."""
    if min_cross_model and not author_family:
        return ("--min-cross-model requires --author-family, else the author's own "
                "family is counted as a cross-model voice (silent no-op)")
    if author_family and author_family.lower().strip() not in KNOWN_FAMILIES:
        return (f"--author-family {author_family!r} is not a known family "
                f"({', '.join(sorted(KNOWN_FAMILIES))}); a wrong value excludes nothing "
                f"and fails open")
    return None


def reduced_coverage(results, author_family, min_cross_model):
    """True when cross-model coverage enforcement is on and too few genuinely
    independent (non-author) families returned a verdict. A Go cap that leaves only
    the author's family + one other trips this even though the panel's own quorum is
    met and it returned a clean verdict."""
    if not min_cross_model:
        return False
    return len(cross_model_families(results, author_family)) < min_cross_model


def gate_exit_code(results, min_quorum_families=MIN_QUORUM_FAMILIES,
                   author_family=None, min_cross_model=None):
    """Fail closed. A gate passes (exit 0) ONLY when the panel neither blocks, falls
    short of a cross-family quorum, NOR has reduced cross-model coverage:
      - `block` -> 1; `no-verdict` (every leg degraded) -> 1.
      - approve/approve-with-notes with fewer than `min_quorum_families` distinct
        families -> 1 (a lone surviving reviewer must not pass for the whole panel).
      - reduced cross-model coverage (fewer than `min_cross_model` non-author
        families voted) -> 1 EVEN with no block — a Go cap silently drops the
        open-weight tier, so this is the load-bearing enforcement.
      - otherwise -> 0."""
    verdict = panel_verdict(results)
    if verdict not in ("approve", "approve-with-notes"):
        return 1
    families = {r["family"] for r in results if r["verdict"]}
    if len(families) < min_quorum_families:
        return 1
    if reduced_coverage(results, author_family, min_cross_model):
        return 1
    return 0


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
    rv.add_argument("--min-quorum-families", type=int, default=MIN_QUORUM_FAMILIES,
                    help="distinct families that must return a verdict for a PASS (exit 0)")
    rv.add_argument("--author-family", default=None,
                    help="the requesting author's model family (e.g. claude); excluded "
                         "from the cross-model coverage count so a same-family voice "
                         "does not inflate it")
    rv.add_argument("--min-cross-model", type=int, default=None,
                    help="require at least N genuinely-independent (non-author) "
                         "families to return a verdict; fewer is reduced-coverage and "
                         "fails closed even with no BLOCK")
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
    if "open-weight" in families:
        bad = [m for m in open_weight_models if m not in OPEN_WEIGHT_ALLOWED]
        if bad:
            print(f"refusing to route non-open-weight model(s) through the gateway: "
                  f"{', '.join(bad)}. Allowed: {', '.join(sorted(OPEN_WEIGHT_ALLOWED))}",
                  file=sys.stderr)
            return 2
    cov_err = validate_coverage_args(args.author_family, args.min_cross_model)
    if cov_err:
        print(f"refusing to run with an unsafe coverage config: {cov_err}", file=sys.stderr)
        return 2
    master_key = load_gateway_secret(GATEWAY_SECRET)
    roster = build_roster(families, open_weight_models, prompt, args.timeout, master_key)

    results = []
    with futures.ThreadPoolExecutor(max_workers=len(roster) or 1) as ex:
        fut = {ex.submit(run_reviewer, n, fam, fn, args.timeout): n for n, fam, fn in roster}
        for f in futures.as_completed(fut):
            results.append(f.result())

    reduced = reduced_coverage(results, args.author_family, args.min_cross_model)
    xmf = sorted(cross_model_families(results, args.author_family))
    if args.format == "json":
        print(json.dumps({"artifact": args.artifact,
                          "panel_verdict": panel_verdict(results),
                          "cross_model_families": xmf,
                          "reduced_coverage": reduced,
                          "reviewers": results}, indent=2))
    else:
        print(render_human(args.artifact, results))
        if args.min_cross_model:
            note = "OK" if not reduced else f"REDUCED-COVERAGE (need {args.min_cross_model})"
            print(f"Cross-model coverage: {len(xmf)} non-author families "
                  f"[{', '.join(xmf) or 'none'}] — {note}")
    return gate_exit_code(results, args.min_quorum_families,
                          args.author_family, args.min_cross_model)


if __name__ == "__main__":
    sys.exit(main())
