# Plan: ADR-0083 implementation — architect verdict via `claude --json-schema`

- **Status:** drafting
- **Date:** 2026-06-07
- **Related ADRs:** ADR-0083 (decision, PR forthcoming), ADR-0082 (superseded), ADR-0048 (verdict surface), ADR-0058 (gate-broken contract)

## Goal

Wire `--json-schema` into the architect's `claude --print` invocation so the verdict envelope is structurally guaranteed at the CLI layer. Read `structured_output` from the result payload first; on absence, emit a `task.architect_emit_failure` event routed via cc-relay to the dispatching orchestrator session. Delete the prose-fallback machinery (`_try_structured_retry`, `_PROSE_VERDICT_CUES`, `_parse_verdict_from_prose`) and the multi-stage `_extract_verdict_envelope` chain that no longer has any reachable branches. The amend-cap counter stays as-is.

## Success criteria

1. The architect's worker-side claude invocation passes `--json-schema "<schema>"` where the schema constrains `verdict` to the enum `[amend, supersede, accept-as-is, gate-broken]` and requires `reasoning` + `target_artifact`. `remediation_summary` and `gate_log_excerpt` are conditional-required via JSON Schema's `oneOf` / `if-then-else`.
2. `_extract_verdict_envelope` reads `structured_output` from the CLI result payload first. Present → return the envelope (with a defensive `verdict in _VALID_VERDICTS` post-check). Absent → emit `task.architect_emit_failure` and return a synthetic envelope with `verdict="emit-failure"` that `handle()` recognizes as a no-dispatch escalation case.
3. The prose-fallback machinery (`_try_structured_retry`, `_PROSE_VERDICT_CUES`, `_parse_verdict_from_prose`, and their tests) is deleted. `ArchitectVerdictParseError` stays defined only for the genuinely unknown-verdict-literal case (line 444 of the existing file).
4. `task.architect_emit_failure` event is shaped in `services/api/treadmill_api/events/task.py`, carries `created_by` (the dispatching label) so cc-relay routes it correctly, and is consumed by a new minimal trigger that drops a relay file into `~/.cc-channels/<label>/relay/` on the worker host. (If the worker host and orchestrator session host are the same machine in v1, drop-and-watch is enough; if not, the event ships via the existing treadmill-events WS feed.)
5. The architect role's existing tests are updated to mock `claude --print --json-schema` returning `structured_output`; new tests cover the emit-failure branch + the deleted-code regression set.
6. AGENT.md entries land for `workers/agent/treadmill_agent/runner_dispositions/AGENT.md` and `workers/agent/treadmill_agent/AGENT.md` (whichever is the closest existing — the worker tree's docs-current-with-pr gate is blocking).
7. The change is reversible by removing the `--json-schema` flag and reverting the disposition. We bake this into the AGENT.md note as the rollback path.

## Constraints / scope

### In scope

- `workers/agent/treadmill_agent/runner_dispositions/architecture.py`: add the schema definition + invoke path; gut the prose-fallback chain; reshape `_extract_verdict_envelope` to `structured_output`-first.
- `workers/agent/treadmill_agent/claude_code.py`: thread the `--json-schema` flag through `run_claude_code` for the architect role's call site (and only that — other roles unchanged).
- `services/api/treadmill_api/events/task.py`: define the `ArchitectEmitFailure` event payload.
- A minimal trigger that turns `task.architect_emit_failure` events into cc-relay drops. Location: `services/api/treadmill_api/coordination/triggers.py` (existing trigger registration pattern).
- Tests at `workers/agent/tests/` covering: schema-conforming output round-trips; `structured_output`-absent emits failure event; deleted prose-fallback paths are not reachable.
- AGENT.md updates per the touched components.

### Out of scope

- **Direct Anthropic API call (Path B).** Joe ruled out for v1; sibling ADR. The agent-loop collapse this enables is a separate concern.
- **The amend-cap counter (`_is_capped`).** Unchanged by this ADR — schema-forcing removes the parse-failure class, so `cap_exempt` machinery is no longer needed. We DO NOT add the `workflow_runs.cap_exempt` column proposed in ADR-0082's plan.
- **Operator-note re-dispatch trigger.** The orchestrator session's decision (hand-author / re-dispatch / escalate) is mediated through cc-relay, not through `operator_note`. The ADR-0081 channel is still valid for worker-initiated context requests; it's just not the architect-emit-failure escalation path.
- **Deterministic detector for stale relay drops.** Out of v1 — handled if residual rate is non-zero post-merge.

### Budget

One PR, ~half-day, hand-authored or worker-dispatched. The disposition layer change is small; the tests are the bulk of the work. No `auto_merge: false` warranted — pure worker-side + a single events payload addition, no shared schema migration, no CDK.

## Sequence of work

```yaml
sequence_of_work:
  - id: architect-json-schema-verdict
    title: "ADR-0083 — wire --json-schema into the architect call; gut the prose-fallback chain"
    workflow: wf-author
    intent: |
      STUDY:
        - docs/adrs/0083-architect-verdict-via-json-schema-flag.md
          for the decision shape. Pay attention to the
          conditional-required fields for supersede and gate-broken.
        - docs/research/2026-06-07-architect-forced-structured-output-spike.md
          for the smoke-test invocation that the architect call must
          mirror.
        - workers/agent/treadmill_agent/runner_dispositions/architecture.py
          for what survives and what gets deleted. Specifically:
          _extract_verdict_envelope (line 315), the four-stage
          fallback chain, _try_structured_retry (line 221),
          _parse_verdict_from_prose (line 151), _PROSE_VERDICT_CUES
          (line 88+), and the in-handle subfailures (line 705, 717).
        - workers/agent/treadmill_agent/claude_code.py for the
          claude --print invocation site. The --json-schema flag is
          conditional on the role being the architect; all other
          roles continue without it (their output kinds are different
          — see ctx.role.output_kind switch).
        - services/api/treadmill_api/events/task.py for the existing
          event payload pattern. The new ArchitectEmitFailure should
          parallel OperatorHintSet in shape and validation.

      BUILD:
        1. Define the verdict JSON Schema inline in architecture.py
           as a module-level constant _VERDICT_SCHEMA. Flat shape —
           the Anthropic tool-schema validator rejects JSON Schema's
           allOf / oneOf / if-then-else (verified 2026-06-07: the
           CLI returns 400 input_schema does not support oneOf when
           the schema uses if-then). Required top-level fields:
           verdict (enum), reasoning, target_artifact,
           remediation_summary. Optional: rewritten_description,
           gate_log_excerpt. Conditional-required validation moves
           into worker code as a post-emit check (step 3).
        2. claude_code.py::run_claude_code: when ctx.role.id ==
           'role-architect', append --json-schema <inline-schema-json>
           to the claude --print argv. Other roles unchanged.
        3. architecture.py::_extract_verdict_envelope: new shape.
             - Read ctx.claude_result.structured_output (a dict or
               None). If absent, emit task.architect_emit_failure
               (path below) and return a synthetic envelope
               {"verdict": "emit-failure", "model_output_excerpt":
               <first 2KB of result + summary>}.
             - If present, post-emit-validate in worker code:
                 * verdict in _VALID_VERDICTS (defensive)
                 * if verdict == 'supersede', rewritten_description
                   non-empty
                 * if verdict == 'gate-broken', gate_log_excerpt
                   non-empty
               Any check fails → emit task.architect_emit_failure
               with a parse_failure_reason discriminator
               (no-structured-output | supersede-missing-rewrite |
               gate-broken-missing-excerpt | invalid-verdict-literal),
               return the synthetic emit-failure envelope.
             - On success, return structured_output verbatim.
           Emit path uses the worker's existing api_client +
           treadmill_api hint-channel POST pattern; see
           worker_hints.py for the canonical shape.
           ArchitectVerdictParseError keeps the line-444 raise (for
           unknown verdict literals from non-architect call sites),
           but is never raised from _extract_verdict_envelope.
        4. handle() branch: before the existing accept-as-is / amend
           / supersede / gate-broken switch, recognize verdict ==
           'emit-failure'. Emit StepOutput with decision='emit-failure',
           no dispatch payload, payload carries the model_output_excerpt
           + the cc-relay drop confirmation.
        5. Delete _try_structured_retry, _parse_verdict_from_prose,
           _PROSE_VERDICT_CUES, _RETRY_PROMPT, _find_claude_binary.
           Update _extract_verdict_envelope's docstring to name only
           the structured-output read.
        6. services/api/treadmill_api/events/task.py: add
           ArchitectEmitFailure(BaseModel) with fields:
           model_output_excerpt: str, created_by: str (so the relay
           consumer routes it). Mirror the OperatorHintSet shape.
        7. services/api/treadmill_api/coordination/triggers.py:
           register maybe_drop_relay_on_architect_emit_failure. When
           the event fires, drop a markdown file at
           ~/.cc-channels/<created_by>/relay/<ts>-architect-emit-
           failure-<task_id>.md whose body cites the task, the model
           output excerpt, and a short remediation hint. (The cc-
           relay channel server on the orchestrator session host
           inotify-picks it up.) For the dev_local deployment where
           worker and orchestrator are on the same machine, this is
           a file write; for production split deployments the same
           code path requires a remote drop mechanism — out of v1
           scope, flag in AGENT.md.

      TEST:
        - workers/agent/tests/test_architect_verdict.py:
          * test_structured_output_present_returns_envelope: stub
            claude_result.structured_output = {verdict:amend, ...},
            assert _extract_verdict_envelope returns it.
          * test_structured_output_absent_emits_failure: stub
            structured_output = None, assert
            task.architect_emit_failure event posted + synthetic
            envelope with verdict='emit-failure' returned.
          * test_structured_output_invalid_verdict_emits_failure:
            stub structured_output = {verdict: 'invalid'}, assert
            same failure path (defensive check).
        - workers/agent/tests/test_runner_dispositions_architecture_*:
          * test_handle_emit_failure_branch: stub envelope with
            verdict='emit-failure', assert StepOutput shape, no
            dispatch payload.
        - DELETE workers/agent/tests/test_review_disposition_prose_synthesis.py
          (the test file for the prose-cue table; verify with grep
          that no other tests import _PROSE_VERDICT_CUES or
          _parse_verdict_from_prose before deletion).
        - services/api/tests/test_architect_emit_failure_event.py
          (new): assert the event payload validates; the trigger
          drops a relay file with the expected content.

      DOC: workers/agent/treadmill_agent/runner_dispositions/AGENT.md
      + workers/agent/treadmill_agent/AGENT.md (or whichever is the
      closest existing) — Recent-changes entry citing ADR-0083:
      'architect emits via --json-schema; prose-fallback chain
      deleted; emit-failure routes to dispatching orchestrator via
      cc-relay; rollback by removing the flag.'

      Validation MUST NOT call live Anthropic, live AWS, docker, or
      live network. All claude_result fixtures are stubs; the
      claude --print invocation is verified via argv assertion in a
      run_claude_code unit test, not by actually invoking the
      binary.
    scope:
      files:
        - workers/agent/treadmill_agent/runner_dispositions/architecture.py
        - workers/agent/treadmill_agent/claude_code.py
        - workers/agent/tests/test_architect_verdict.py
        - workers/agent/tests/test_runner_dispositions_architecture_nothing_to_do.py
        - workers/agent/tests/test_review_disposition_prose_synthesis.py
        - services/api/treadmill_api/events/task.py
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/tests/test_architect_emit_failure_event.py
        - workers/agent/treadmill_agent/runner_dispositions/AGENT.md
        - workers/agent/treadmill_agent/AGENT.md
      services_affected:
        - workers/agent
        - services/api
      out_of_scope:
        - Direct Anthropic API call (sibling ADR / Path B)
        - workflow_runs.cap_exempt column from the prior ADR-0082 plan
        - operator_note re-dispatch trigger from the prior ADR-0082 plan
        - Production-split relay-drop mechanism (dev_local only in v1)
    validation:
      - kind: deterministic
        description: |
          New architect-verdict tests pass; existing runner-disposition
          tests stay green; deleted prose-cue tests are removed without
          orphaning imports; the architect emit_failure event roundtrips
          through the trigger.
        script: |
          cd workers/agent && uv run pytest tests/test_architect_verdict.py tests/test_runner_dispositions_architecture_nothing_to_do.py -q && cd ../../services/api && uv run pytest tests/test_architect_emit_failure_event.py tests/test_coordination_triggers.py -q
        severity: blocking
        timeout_seconds: 240
      - kind: llm-judge
        description: |
          AGENT.md Recent-changes entries cite ADR-0083, name the
          --json-schema wedge, and document the rollback path.
        prompt: |
          The DIFF must include Recent-changes entries under the
          worker tree's AGENT.md files that cite ADR-0083, name the
          --json-schema flag as the wedge, and call out the rollback
          path (remove the flag + revert the disposition). Return
          'pass' when all three present; 'fail' otherwise.
        severity: blocking
```

## Risks / unknowns

- **Conditional-required schema fields RESOLVED 2026-06-07 smoke.** The Anthropic tool-schema validator rejects `allOf` / `oneOf` / `if-then-else` with a 400 error. Conditional-required validation lives in worker code as a post-emit check, NOT in the schema text. This is functionally equivalent but slightly less safe — the model can emit a malformed envelope; the worker catches it immediately and routes to emit-failure. Same recovery path, same orchestrator-via-cc-relay escalation surface.
- **`structured_output` shape across CLI versions** is unspecified for our purposes. If the CLI deprecates the field or moves it, the architect's verdict path breaks silently. Mitigation: the worker fails the step loudly when `structured_output` is the wrong shape, surfacing the regression. Schema validation in-code is the second line.
- **cc-relay drop on production split deployments.** In `dev_local` the worker and orchestrator are colocated on the same host, so a file-drop is enough. On a production split, the trigger needs a remote-drop mechanism (SQS message to the orchestrator host?). Flagged in AGENT.md; out of v1 scope but documented so the gap is visible.
- **The architect agent loop is unchanged by this PR.** Tokens are still ~25K average per architect run. Path B (sibling ADR) is the optimization; surfacing here so the cost line is honest.

## Decisions captured during execution

_Empty._

## Post-mortem

_Filled on completion._
