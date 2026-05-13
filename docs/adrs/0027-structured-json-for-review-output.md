# ADR-0027 — Structured JSON envelope for review-kind step output

* Status: Proposed (drafted 2026-05-12)
* Supersedes / amends: ADR-0022 §"`review` output kind"
* Companion: ADR-0011 (uniform envelope), ADR-0012 (decision strings)
* Trigger: live failure on PR #10 (2026-05-12) — the role emitted
  `**VERDICT: request_changes**`, the strict regex
  (`workers/agent/treadmill_agent/runner_dispositions/review.py:34-37`)
  rejected it, the verdict defaulted to `comment`, ADR-0013's
  mergeability VIEW collapsed `needs-more-info` to
  `blocked-on-review`, and the runner re-authored against the same
  SHA. This was the proximate cause of the PR #10 deathloop. A
  tourniquet (commit `8ff414c`) widens the regex to tolerate
  common Markdown decorations; this ADR is the durable fix.

## Context

ADR-0022 introduced output kinds — the `review` kind handler greps
Claude's last `VERDICT:` line for one of three verbs
(`approve` / `request_changes` / `comment`) and passes it to
`gh pr review`. The marker is plain prose; the handler is a regex.

Every other output kind in Treadmill consumes a structured envelope:

* `code` kind reads commit SHA + decision + task directive from the
  uniform `StepOutput` envelope (ADR-0011, ADR-0012).
* `analysis` kind reads `decision` + `payload` directly from
  `StepOutput`.
* `plan_doc` kind reads the plan doc's YAML frontmatter (structured
  by definition).

The review handler is the **only** place where a runner-consumed
field — the verdict — survives as free-text prose the regex has to
re-derive. That asymmetry is the fragility.

The PR #10 failure showed the regex couldn't survive the model's
emphasis instructions; the tourniquet narrows the failure surface
but doesn't close it. The model can still emit `VERDICT: looks-good`,
or wrap the marker in a more exotic decoration, and the parser will
silently fall through to `comment`.

This ADR replaces the prose marker with a Pydantic-validated JSON
envelope, restoring the "Pydantic at every boundary" discipline
ADR-0011 set down for the rest of the system.

## Decision

The role-reviewer prompt instructs Claude to end its output with a
fenced JSON object matching the `ReviewVerdict` Pydantic model. The
disposition handler parses that block and validates the verdict
against the closed value-set; an invalid or absent JSON block falls
through to the existing regex tourniquet (defense in depth during
the transition), then to the safe `comment` default.

```json
{
  "verdict": "approve" | "request_changes" | "comment",
  "rationale": "<one-paragraph human-readable why>"
}
```

The fenced block is the **only** structured channel; the surrounding
prose remains the human-facing review body that `gh pr review`
posts. The handler:

1. Greps the output for the **last** ```` ```json ... ``` ```` block.
2. Attempts `json.loads` → `ReviewVerdict.model_validate(...)`.
3. On success, the verdict + rationale flow into the existing
   `StepOutput.payload` + `decision` + `Artifact("pr_review", verdict)`
   path. The fenced block is stripped from the prose body that's
   posted as the PR review (the JSON shouldn't be visible to the
   reader).
4. On parse / validation failure, log a structured warning
   (`review.json_parse_failed`) and fall through to the existing
   tourniquet regex. The warning is the trip-wire for "the model
   drifted; investigate."

Why a fence and not a final JSON line: model behavior under "end
with this exact JSON" is more reliable when the format is a code
fence (matches the model's training data shape — markdown blogs,
chat answers, code reviews). Bare JSON lines mid-prose are a known
drift pattern.

## Consequences

* Verdict parsing becomes a single `model_validate` call, not a
  regex against prose — the kind of boundary ADR-0011 says we
  should have everywhere.
* The closed value-set is enforced by Pydantic, not by hand-edit of
  a regex alternation. Adding a new verdict requires a deliberate
  model field change.
* Prose body and structured verdict are separately addressable:
  rationale fields can be surfaced in dashboards or future
  ralph-loop validation without re-parsing the review body.
* The tourniquet's regex stays as a fallback for ~one release cycle
  so a single drifted output doesn't deathloop us. Removal happens
  once we have data showing the model holds the JSON shape reliably.
* `gh pr review --body <text>` — the body argument loses the fenced
  block (the handler strips it before posting). The PR-page reader
  sees clean prose; the runner sees the verdict.

## Alternatives considered

### A. Widen the regex permanently (the tourniquet, kept long-term)
Cheap, no prompt change. Rejected as the durable fix because every
new decoration the model invents is a parser bug. The fragility class
is unchanged.

### B. Outsource verdicts to GitHub Copilot's review API
Removes the parsing surface entirely, but Copilot's verdict
vocabulary doesn't map cleanly onto ADR-0012's three-state
value-set (`approved` / `changes_requested` / `needs-more-info`).
We'd write a translation layer with the same parsing-fragility class
of bug, just farther from the prompt. Also: external dependency, new
authn surface, opinionated review style we can't control.

### C. Free-text marker but `claude --output-format json`
Use Claude Code's JSON output mode so the *envelope* is structured
even when the model emits prose. Would require auditing every
output kind, since `claude --output-format json` reshapes the entire
turn structure — not just the review case. Banked as a separate
investigation (ADR-?? — output-format JSON across all kinds).

## Open Qs (for operator review)

* **Q27.a — Fallback retention period.** When do we delete the
  tourniquet regex? Default: one full smoke cycle's worth of
  reviews land cleanly via JSON before removal. Looking for a more
  concrete bar (e.g., "10 consecutive runs without falling to the
  regex path") or a time-based one.
* **Q27.b — Rationale length cap.** Should `rationale` be capped
  (e.g., `max_length=2000`) in the Pydantic model, or is the model
  reasonable enough that we trust it? Cap is cheap insurance against
  a runaway model; uncapped is less prompt-engineering pressure.
* **Q27.c — Strip-the-fence policy.** When the handler strips the
  JSON fence from the body it posts to GitHub, should it leave a
  marker so reviewers know the structured channel exists (e.g.,
  "Verdict: request_changes (parsed from structured block)")? Or
  keep it invisible? Leaning invisible — the verdict's effect on
  mergeability is the operator-visible signal already.
* **Q27.d — Validation under `--dry-run`.** The dry-run path skips
  `gh pr review`. Should it also skip JSON parsing (to allow
  experimentation with prompt-only changes) or run the parser to
  surface drift in test? Leaning "always parse, always log" — the
  drift signal is the value.

## Phasing

1. Add the Pydantic model + parser to `runner_dispositions/review.py`.
   Wire it ahead of the regex tourniquet (JSON tried first, regex
   second, safe default last).
2. Rewrite the `role-reviewer` prompt in
   `services/api/treadmill_api/starters.py` to instruct the JSON
   fence. Re-seed via the existing `workflows seed-starters` path —
   though note ADR-0028 (forthcoming) intends to flip the
   source-of-truth so prompt changes don't need a code edit.
3. Land tests for the JSON path + tests for the regex-fallback path
   (so we can see parse-failure → tourniquet behavior in CI).
4. Run a smoke; collect drift-warning counts for the first 10+
   review runs.
5. After Q27.a's bar is met, delete the tourniquet regex.
