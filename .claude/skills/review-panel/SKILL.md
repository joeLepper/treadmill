---
name: review-panel
description: Run the cross-model review panel over an artifact — an ADR, a plan, a design note, a diff, or a whole PR — to get an adversarial verdict from models spanning three provider families (open-weight, gpt, claude) in one shot. Use before merging a PR, before an ADR/plan settles, when asked to "run the panel", "cross-model review this", "gate this", or whenever you want an independent read from models outside your own family. Fails closed: a block, a degraded panel, or a one-model pass never reads as approval. Runs from Claude Code and Codex alike.
---

# /review-panel — cross-model review panel

Fan one artifact out to a panel of models across THREE provider families and collect an
adversarial review from each — single-shot calls, not agentic sessions. The panel verdict
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
  [--format human|json] [--timeout 240]
```

`--artifact` is any single file: an ADR, a plan, a design note, or a `.diff`.

## Run it — on a PR or a branch (the helper)

`scripts/review-pr.sh` builds the diff artifact for you (excluding generated/vendored
files that would only pad the review), then runs the panel. Run it from inside the target
repo's working tree:

```
<skill-dir>/scripts/review-pr.sh <PR-number | branch | file> \
  [--base <ref>]            # merge-base ref; default origin/main
  [--author-family <fam>]   # your own family, excluded from coverage (e.g. claude)
  [--json]                  # machine-readable panel output
  [--exclude <glob>]        # extra pathspec exclude; repeatable
```

Examples:
- `scripts/review-pr.sh 9189 --author-family claude` — review PR #9189 (via `gh`).
- `scripts/review-pr.sh joe/forecast-precompute` — review a local/remote branch vs origin/main.
- `scripts/review-pr.sh /tmp/my.diff` — review a diff file directly.

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

- **No API keys.** open-weight → fleet gateway (OpenCode Go budget); gpt → `codex exec`
  OAuth in an isolated `CODEX_HOME`; claude → `claude -p` on the subscription (the panel
  unsets `ANTHROPIC_API_KEY` so it never bills the API).
- **The artifact is untrusted input.** Both paid legs are fail-closed — the codex leg runs
  read-only with no approval, the claude leg grants NO tools via a positive allowlist. You
  can safely panel a diff you did not write.
- **Prerequisite:** `model-gateway.service` must be active for the open-weight leg. If it is
  down, that family degrades; gpt and claude still return, and the quorum rule decides
  whether that is enough coverage.

See `tools/model-review-panel/AGENT.md` for the full routing table and guarantees, and
ADR-0107 (panel) / ADR-0105 (cross-model pass) / ADR-0103 (one canonical skill source).
