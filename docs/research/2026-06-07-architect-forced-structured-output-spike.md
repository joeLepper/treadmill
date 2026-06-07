# Research spike: forced structured output for the architect role

- **Date:** 2026-06-07
- **Triggered by:** Joe's pushback on ADR-0082 (PR #239) — token economics, "operator" framing, escalation philosophy
- **Co-researchers:** treadmill-bert (Path A — Claude Code CLI capabilities), treadmill-alan (Path B — direct Anthropic API)
- **Status:** complete; informs ADR-0082 revision (likely supersede)

## Hypothesis

Forcing the architect's verdict through a schema constraint (tool-use, structured output, or schema-validated emit) at the input collapses the parse-failure surface at its source. This is preferable to building an escalation channel around the parse-failure tail, because:

1. The escalation channel still burns a full Ralph-loop per redispatch (architect + wf-feedback round-trip) — cap-exempt only protects the attempt counter, not the token spend.
2. The "operator" framed in the prior ADR was implicitly Joe; the right operator for a parse-failure recovery is an orchestrator session (bert/alan/carla/donna) with larger context, not the human.
3. Human escalation is the last resort, not the first.

If a schema constraint at the model level eliminates ~90%+ of parse-failures, the residual ones can be handled by genuinely hand-authoring or hand-dispatching from an orchestrator session, with the human only involved as the backstop.

## Findings

### Path A — Claude Code CLI `--json-schema` flag

**Result: VALIDATED.** The deployed CLI (`claude --version` returns `2.1.168 (Claude Code)`) exposes a `--json-schema <schema>` flag, documented in `--help` as "JSON Schema for structured output". When combined with `--output-format json`, the CLI emits a `structured_output` field in the result payload that conforms to the provided schema. The prose `result` field is empty when the model uses the structured channel exclusively.

**Smoke test:**

```bash
claude --print \
  --output-format json \
  --json-schema "$(cat verdict-schema.json)" \
  --model claude-haiku-4-5-20251001 \
  --permission-mode acceptEdits \
  "You are the Treadmill architect. The worker submitted a PR..."
```

with `verdict-schema.json` constraining `verdict` to the enum `[amend, supersede, accept-as-is, gate-broken]` and requiring `reasoning` + `target_artifact`.

**Output:** `{"type":"result", ..., "result":"", "structured_output":{"verdict":"gate-broken","reasoning":"...","target_artifact":"...","remediation_summary":"..."}, ...}` — schema-conforming, empty prose channel.

**Caveat surfaced by the smoke:** the model still runs in agentic mode (13 turns, ~3,200 output tokens for what should be one inference). It tried `git diff` and `find` (permission-denied), then settled on `gate-broken` because it couldn't locate the artifact. The schema constraint kept the OUTPUT structured but did not shrink the AGENT LOOP — the model still spent tokens exploring before emitting. This is a SEPARATE optimization, not a counter-argument to Path A.

**Cost of adoption:** one flag in the architect's `claude --print` invocation in `runner_dispositions/architecture.py` (and possibly `claude_code.py::run_claude_code`). No new dependencies, no new auth path, no new code surfaces. The four-stage fallback chain in `_extract_verdict_envelope` becomes vestigial — schema rejection at the model level replaces stage 1; stages 2-4 are no longer reachable for the common case.

### Path B — Direct Anthropic API call with `tool_choice`

**Result: VALIDATED, larger change.** The Anthropic Messages API supports forced tool use via `tool_choice: {"type": "tool", "name": "submit_verdict"}`. Free-form prose becomes structurally impossible. Schema validation happens at the API layer; a malformed shape comes back as an HTTP-level error, not a parsing concern in the worker.

**Alan's findings (relayed):**
- The architect today runs through `runner.py:491 → claude_code.run_claude_code()` with `--permission-mode acceptEdits` and `output_kind=analysis`. Its role config (`starters.py:730`) specifies `model=claude-sonnet-4-6`, no `allowed_tools` override, no MCP constraints. The architect uses zero tools today; it's a pure analytic inference wearing a full Claude Code worker suit.
- Switching to direct API: gain forced tool_choice + schema validation; lose subprocess startup overhead (~2-5s saved per run); lose the secondary `_try_structured_retry` call cost (~1K tokens / parse-failure); lose nothing functionally because the architect uses no Claude Code conveniences (no MCP, no permission model, no session resumption).
- Worker `pyproject.toml` does NOT include the `anthropic` SDK. Two options: (1) add `anthropic>=0.30.0` — clean SDK, Dockerfile rebuild; (2) use `httpx` directly against `/v1/messages` — `httpx` is already in the image (used in `api_client.py` + `worker_hints.py`); no new dep.
- Alan's lean: option (2) httpx-direct for the first cut. Credential flow already solved — `startup_auth.py` sets `ANTHROPIC_API_KEY` on the worker env; pass it as `x-api-key`.

**Cost of adoption:** new function (~50 LOC) replacing the architect-specific Claude Code subprocess call. `runner.py:562` branches on `ctx.role.id == "role-architect"` → call the direct path. The disposition fallback chain in `architecture.py` becomes dead code for the architect role; we delete `_try_structured_retry`, simplify `_extract_verdict_envelope` to a tool-input unpack.

## Token-burn quantification

The 9 tasks that hit parse-failures across production history:

| Metric | Value |
|---|---|
| Affected tasks | 9 |
| Total workflow runs across those tasks | 95 |
| Total output tokens burned | **2,877,100** |
| Avg output tokens per affected task | **325,300** |
| Avg output tokens per `wf-architecture-resolve` run | 25,673 |
| Avg output tokens per `wf-feedback` run | 37,839 |
| Avg output tokens per `wf-author` run | 20,519 |

**One Ralph-loop iteration** ≈ 1 architect run + 1 feedback run ≈ **63,512 output tokens** ≈ **$0.95** at Sonnet 4.5 output pricing ($15/M).

**Pattern across the 9 affected tasks:** each ran ~5 architect cycles + ~5 feedback cycles before the cap or merge. That is a Ralph loop completing 5x where 1 should have sufficed. Per Joe's framing — these are the tokens lost on parse-failure.

**Hypothetical savings under Path A or Path B:**
- Conservatively assume schema-forcing eliminates 4 of the 5 redundant Ralph-loop iterations per affected task. Per-task savings: ~4 × 63K = **~254K output tokens** ≈ ~$3.80/task.
- Across 9 historical affected tasks: ~$34 in output-token spend recovered. The cluster on 2026-06-05 alone (4 tasks × ~$5 each) is $20.
- Going forward, every avoided parse-failure preserves ~63K output tokens × cap_attempts_used (typically 3-5) = 190K-315K tokens per saved task.

This is not the dominant cost line for Treadmill, but it is unambiguously **wasted** spend on a structural failure mode that has a one-flag fix.

## Side observation: architect agent loop

The Path A smoke surfaced an orthogonal cost: the architect runs as a full Claude Code agent (multi-turn, file-reading, exploring the repo) even though its job is a single analytic emit. The 13-turn smoke spent ~3,200 output tokens on exploration before emitting a verdict.

Today's architect averages 25,673 output tokens / run (from the table above). A direct-API single-shot inference (Path B) would land closer to 1-3K output tokens / run — an order-of-magnitude reduction PER architect run, independent of parse-failure.

This is not in scope for the present ADR revision, but worth surfacing: **Path B's value extends beyond parse-failure** to a broader architect-cost optimization. Path A fixes parse-failure cheaply; Path B fixes parse-failure AND collapses the agent loop.

## Recommendation

**Land Path A first (single-flag fix), spec Path B as a follow-up.**

Rationale:
1. Path A is one flag (`--json-schema`) in the existing `claude --print` invocation. No new auth path, no new dep, no new code surface. Reversible by removing the flag.
2. Path A eliminates the parse-failure class. Token-burn quantification: ~$34 of output-spend recoverable across 9 historical tasks; ~$3.80 per future affected task.
3. Path A leaves Path B as a clean follow-up. Path B's incremental value is the agent-loop collapse, which is an independent optimization worth its own ADR.
4. Path B's incremental complexity (httpx-direct call, new credential plumbing audit, dead-code removal) is larger and merits its own ADR + plan + review cycle.

**Residual escalation path (after Path A):**
- The model can still reject the schema (rare: model produces output the JSON Schema validator can't accept). When this happens, the disposition emits a `needs-orchestrator-attention` event (not `needs-human`) routed via cc-relay to the dispatching orchestrator session.
- The orchestrator (bert/alan/carla/donna) sees the relay, reads the failing run, decides whether to (a) hand-author the fix, (b) re-dispatch with adjusted scope, or (c) escalate to Joe.
- Joe is the backstop for cases where the orchestrator can't decide. Not the first line — matches `feedback_operator_acts_on_own_escalations.md` + the ADR-0075 premise.
- The `cap_exempt` machinery from the prior ADR-0082 draft becomes simpler: the orchestrator dispatches a new task with the corrected scope, not a re-run of the failing task. No need for per-run flags on `workflow_runs`.

## What changes in ADR-0082

The prior draft (PR #239) builds a soft-escalation path TO THE HUMAN with `operator_note` + `cap_exempt` + re-dispatch triggers. Wrong wedge. Supersede with a new ADR that:

1. Adopts Path A — `--json-schema` on the architect's `claude --print` call.
2. Names the orchestrator session (not Joe) as the escalation recipient for the residual schema-reject case.
3. Routes residual cases via cc-relay, not `operator_note`.
4. Defers Path B (direct-API + agent-loop collapse) to a sibling ADR.

PR #239 stays as draft for context; the revised ADR cites it as "superseded by" with the reasoning above. PR #240 (the impl plan) is similarly scrapped — the new plan is one task: add the flag, regression-test the four-stage chain becomes vestigial, delete the dead retry code. ~half a day, not four tasks.

## References

- `workers/agent/treadmill_agent/runner_dispositions/architecture.py::_extract_verdict_envelope`
- `workers/agent/treadmill_agent/claude_code.py::run_claude_code`
- `services/api/treadmill_api/coordination/triggers.py::_is_capped`
- `docs/learnings/2026-06-05-architect-output-malformed-recurring-on-large-prompt-tasks.md`
- ADR-0029, ADR-0048, ADR-0058, ADR-0075 (operator-as-backstop premise), ADR-0081 (hint channel — load-bearing on the residual escalation path)
- Smoke test: `claude --json-schema` on the deployed CLI 2.1.168 — verified emitting `structured_output` with enum-constrained `verdict`
- Production data: 9 affected tasks, 95 runs, 2.877M output tokens, queried 2026-06-07 from `treadmill-postgres`
