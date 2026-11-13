---
name: review-panel
description: Run the cross-model review panel over an artifact — an ADR, a plan, a design note, a diff, or a whole PR — to get an adversarial verdict from models from distinct provider families, through CLI, gateway or Netlify Agent Runner legs. Use before merging a PR, before an ADR/plan settles, when asked to "run the panel", "cross-model review this", "gate this", or whenever you want an independent read from models outside your own family. Fails closed: a block, a degraded panel, or a one-model pass never reads as approval. Runs from Claude Code and Codex alike.
---

# /review-panel — cross-model review panel

We send one artifact to models from distinct provider families and collect an
adversarial review from each. Default legs use single-shot calls; optional AR legs
each start one bare Netlify Agent Runner. The panel verdict
is the WORST reviewer verdict (`block` > `approve-with-notes` > `approve`). It **fails
closed**: exit 0 only on a non-block verdict backed by a real cross-family quorum. Silence,
total failure, or a lone surviving reviewer all exit non-zero. This is ADR-0107's primitive
on top of the fleet model gateway.

Use it two ways: **on demand** while authoring or before you merge, and as a **workflow
gate** (below). It runs the same from Claude Code and Codex — the actionable path is a
shell command, so no harness-specific tool is required.

## When to invoke

- Before you open or merge a PR and want an independent cross-model read.
- Before an ADR or plan settles (the ADR-0105 cross-model pass).
- When the operator or a sibling says "run the panel", "cross-model review this", "gate it".
- When you authored something and want a voice from OUTSIDE your own model family — pass
  `--author-family` so your own family is not counted toward independent coverage.

Do NOT use it as a substitute for your own adversarial read (`adversarial-review`) — it is a
second layer, not the first. A green panel on code you never ran is still unverified.

## Run it — on a file

```
python3 ~/treadmill/tools/model-review-panel/panel.py review --artifact PATH \
  [--families open-weight,gpt,claude] [--author-family claude] \
  [--kind code|diff|adr|plan|design] \
  [--format human|json] [--timeout 240]
```

`--artifact` is any single file: an ADR, a plan, a design note, or a `.diff`.

## Pick the artifact kind (`--kind`)

`--kind` tells the panel WHAT it reviews, so a decision record is not judged as code.

- `code` / `diff` (default) — judge the implementation. The full rubric applies.
- `adr` / `plan` / `design` — judge the DECISION, not the implementation. The panel adds a
  rubric addendum: a missing or under-specified mechanism is NON-BLOCKING for the document.
  The panel blocks only on a decision defect — a contradiction, an unfalsifiable claim, a
  false factual claim, a violation of a cited ADR invariant, or a wrong decision. A sound
  decision with open mechanism questions earns `approve` or `approve-with-notes`, not `block`.

Use `adr`/`plan`/`design` for an ADR-0105 cross-model pass, so the panel stops over-blocking
an ADR for leaving implementation detail to the plan. If you omit `--kind`, the panel infers
it from the artifact path — but **only a Markdown doc (`.md`) under an `adrs/` or `plans/`
directory** infers a decision kind. A CODE file under such a directory (e.g. `src/plan/x.py`)
stays `code`, so its implementation bugs still block; the relaxation never fails open on real
code. The panel prints the inferred kind to stderr. `review-pr.sh` sets it for you.

Inference lives in ONE place (`panel.py infer_kind`), and only the PLURAL convention dirs
(`adrs/`, `plans/` — what `/decide` and `/author` write) relax; a singular `adr/`/`plan/`
dir or an instruction/skill markdown such as `.claude/skills/plan/SKILL.md` stays `code`.
`review-pr.sh` copies the artifact to a `.diff` temp, so it passes the original path via
`--infer-from` and lets `panel.py` decide — the helper never does its own inference. The panel
records the kind it used: the human output prints `Artifact kind: …` and the JSON carries
`kind` + `decision_rubric`, so a gate can tell a relaxed decision pass from a strict code pass. Note: a PR/branch artifact is reviewed as a `diff`
(the code rubric). If a PR changes ONLY decision docs and you want the decision rubric, pass
`--kind adr` (or `plan`/`design`) to the helper explicitly, or review the file directly.

## Run it — on a PR or a branch (the helper)

`scripts/review-pr.sh` builds the diff artifact for you (excluding generated/vendored
files that would only pad the review), then runs the panel. Run it from inside the target
repo's working tree:

```
<skill-dir>/scripts/review-pr.sh <PR-number | branch | file> \
  [--base <ref>]            # merge-base ref; default origin/main
  [--author-family <fam>]   # your own family, excluded from coverage (e.g. claude)
  [--families <list>]       # panel legs, e.g. gpt-ar,claude
  [--timeout <seconds>]     # AR legs have a minimum timeout budget of 1200 seconds
  [--kind <kind>]           # code|diff|adr|plan|design; default diff for a PR/branch,
                            #   inferred from the path for a file (see "Pick the artifact kind")
  [--json]                  # machine-readable panel output
  [--exclude <glob>]        # extra pathspec exclude; repeatable
```

Examples:
- `scripts/review-pr.sh 9189 --author-family claude` — review PR #9189 (via `gh`).
- `scripts/review-pr.sh joe/forecast-precompute` — review a local/remote branch vs origin/main.
- `scripts/review-pr.sh /tmp/my.diff` — review a diff file directly.
- `scripts/review-pr.sh /tmp/my.diff --families gpt-ar,claude` — combine an AR leg with a CLI leg.

The helper prints the panel's human output and exits with the panel's own exit code, so it
drops straight into a gate.

## Reading the result

- **Exit 0** — the panel did NOT block AND a cross-family quorum returned a real verdict.
  Safe to treat as a panel pass.
- **Exit non-zero** — one of: a `block` verdict (some family found a blocking defect), a
  `no-verdict` degraded panel (legs errored/timed out — never read as approval), or an
  approve that lacked the quorum (`reduced_coverage`, e.g. only one family survived — often
  the open-weight leg hit the OpenCode Go 5-hour cap and returned HTTP 401; that is a rate
  cap that resets, not a dead key). Read the per-reviewer `review` bodies for the findings;
  a blocking finding names the invariant and the line.

A block is a signal to inspect, not an order to obey — verify the finding against the code
(the panel's reviewers are context-blind and can over-fire). Confirm it the way
`adversarial-review` demands before you act on it.

## As a workflow gate

The fail-closed exit code IS the gate. In a plan's `validation` step:

```yaml
validation:
  - kind: deterministic
    description: Cross-model review panel does not block this change.
    script: |
      /home/joe/skills-canonical/.claude/skills/review-panel/scripts/review-pr.sh \
        "$PR_NUMBER" --author-family claude
    severity: blocking
```

The gate passes only on exit 0 (non-block + quorum). A block, a degraded panel, or a
one-model pass fails it closed. Reference the canonical skill path so every harness runs the
same versioned copy (ADR-0103), never a per-worktree copy.

**The panel only sees what the artifact contains.** `review-pr.sh` excludes generated/vendored
paths (`*.baseline.json`, `dist/`, `build/`, lockfiles, `*.min.*`, snapshots) so they do not pad
the review — but a change that lives ONLY in an excluded path is never seen by the panel, so the
gate cannot vouch for it. Pass `--exclude`/`--base` deliberately, and do not rely on the gate for a
supply-chain-shaped change hidden in a lockfile or a minified file. One edge: when a PR's head branch
cannot be resolved, the helper falls back to `gh pr diff`, which applies NO exclusions (it announces
this on stderr) — so that path reviews a different, wider scope than the branch/PR-fetch path.

## Routing, auth, and safety (inherited from the panel)

We select legs with `--families`: `open-weight`, `gpt`, `claude`, `gpt-ar`,
`gemini-ar` or `claude-ar`. An unknown name exits 2 and lists the known names.
The helper passes this option to the panel. AR legs count as their model family,
not an extra family: `gpt,gpt-ar` still counts as one. `gemini` is a valid
`--author-family`.

| Leg | Route and model | Credentials and billing |
| --- | --- | --- |
| open-weight | Fleet gateway | OpenCode Go budget; requires `model-gateway.service` |
| gpt | `codex exec` in an isolated `CODEX_HOME` | Codex OAuth |
| claude | `claude -p` | Subscription; `ANTHROPIC_API_KEY` is unset |
| gpt-ar | Agent Runner codex, gpt-6.1-sol, high effort | Netlify token and credits |
| gemini-ar | Agent Runner gemini, gemini-3.1-pro-preview, high effort | Netlify token and credits |
| claude-ar | Agent Runner claude, runner default model, high effort | Netlify token and credits |

AR authentication uses `NETLIFY_AUTH_TOKEN`, else the Netlify CLI's current login
(`netlify login`). The token is never printed. The default site is
`fleet-ar-review-testing` in `netlify-testing`, which pays the Netlify credits.
It has no repository, environment variables or functions. `PANEL_AR_SITE_ID`
selects another site; check that site's contents and billing before using it.
Artifact text goes to Netlify Agent Runners, and `gemini-ar` reaches Google through
them. Each gets a read-only brief plus the same rubric and artifact as the other
legs; its final message supplies the review.

The gpt CLI leg uses read-only restrictions and no approvals; the claude CLI leg
allows no tools. Every AR leg starts in Ask mode (`mode: "ask"`): the runner
applies a read-only permission profile for Codex, deny rules for Claude and Gemini,
read-only database access and no deploy. The read-only brief remains. The sandbox
still carries a Netlify API token and the AI Gateway key; an empty review site
does not mean there are no credentials to read.

An AR leg gets at least 1,200 seconds, even when `--timeout` is lower. It returns
sooner when done; an observed 335-line gpt-ar review took about 75–85 seconds.
An error, cancellation, timeout, input request (`await_input`), still-active run
at the deadline, or more than three consecutive status-read failures degrades the
leg and never approves. A leg that gives up tries to cancel its nonterminal runner.
If cancellation fails, it warns with `netlify agents:stop <id>`; stop that runner
to end further spending. A completed run still needs review text and a verdict.

Ctrl-C or SIGTERM makes the panel attempt to cancel every runner a leg is still
waiting on. It then exits 130 with `panel: interrupted; Agent Runner legs were cancelled`.
Check for cancellation warnings: that final message does not mean a failed
cancellation succeeded. Use the printed stop command for any such runner.

See `tools/model-review-panel/AGENT.md` for the full routing table and guarantees, and
ADR-0107 (panel) / ADR-0105 (cross-model pass) / ADR-0103 (one canonical skill source).
