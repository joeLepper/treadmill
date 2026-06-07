# Plan: ADR-0083 implementation — architect verdict via `claude --json-schema`

- **Status:** drafting
- **Date:** 2026-06-07
- **Related ADRs:** ADR-0083 (decision, PR forthcoming), ADR-0082 (superseded), ADR-0048 (verdict surface), ADR-0058 (gate-broken contract)

## Goal

Wire `--json-schema` into the architect's `claude --print` invocation so the verdict envelope is structurally guaranteed at the CLI layer. Read `structured_output` from the result payload first; on absence, emit a `task.architect_emit_failure` event routed via cc-relay to the dispatching orchestrator session. Delete the prose-fallback machinery (`_try_structured_retry`, `_PROSE_VERDICT_CUES`, `_parse_verdict_from_prose`) and the multi-stage `_extract_verdict_envelope` chain that no longer has any reachable branches. The amend-cap counter stays as-is.

## Success criteria

1. The architect's worker-side claude invocation passes `--json-schema "<schema>"` where the schema is FLAT (the Anthropic tool-schema validator rejects `allOf` / `oneOf` / `if-then-else` — verified 2026-06-07). Required: `verdict` (enum), `reasoning`, `target_artifact`, `remediation_summary`. Optional: `rewritten_description`, `gate_log_excerpt`. Conditional-required validation is a worker-side post-emit check.
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

Two PRs (one per task in the split), ~half-day total wall-clock if Alan and I land them in parallel. The disposition layer change is small; the tests are the bulk. No `auto_merge: false` warranted on either task — pure worker-side change + a single events payload addition, no shared schema migration, no CDK. Per Joe's directive 2026-06-07: bert and alan have mutual review-and-merge authority; this plan does not need operator-merge on each PR.

## Shared event-payload contract

The seam between Task A (worker) and Task B (API) is the `task.architect_emit_failure` event. Both tasks must agree on this payload exactly:

```python
class ArchitectEmitFailure(BaseModel):
    """Worker-side emit-failure escalation — routed via cc-relay to the
    dispatching orchestrator session per ADR-0083."""

    parse_failure_reason: Literal[
        "no-structured-output",
        "supersede-missing-rewrite",
        "gate-broken-missing-excerpt",
        "invalid-verdict-literal",
    ]
    model_output_excerpt: str   # first 2KB of result + summary
    created_by: str             # the dispatching session label;
                                # routes the relay drop into
                                # ~/.cc-channels/<created_by>/relay/
    failing_run_id: str         # uuid of the wf-architecture-resolve
                                # run that emitted the failure
```

POST endpoint (worker side): `POST /api/v1/tasks/{task_id}/events` with `entity_type="task"`, `action="architect_emit_failure"`, and the payload above. Mirrors the existing `operator_hint_set` pattern (`services/api/treadmill_api/routers/tasks.py`).

Either task may land first. Tests on each side stub the other.

## Sequence of work

Split into two parallel tasks. Either may land first; each stubs the other in tests. Owners:

- **Task A** (worker side) — Bert (full disposition context cached from the design pass)
- **Task B** (API side) — Alan (cap-counter deep-read precedent makes the events/triggers seam adjacent territory)

```yaml
sequence_of_work:
  - id: worker-json-schema-and-post-emit-validate
    title: "Task A — thread --json-schema on architect calls; read structured_output; post-emit validate; gut prose-fallback chain"
    workflow: wf-author
    intent: |
      STUDY:
        - docs/adrs/0083-architect-verdict-via-json-schema-flag.md
          for the decision shape.
        - docs/research/2026-06-07-architect-forced-structured-output-spike.md
          for the smoke-test invocation the architect call must mirror.
        - The "Shared event-payload contract" section in this plan
          (parent doc) for the ArchitectEmitFailure shape — Task B
          owns the API-side definition; this task POSTs that exact
          shape.
        - workers/agent/treadmill_agent/runner_dispositions/architecture.py
          for what survives and what gets deleted: _extract_verdict_envelope
          (line 315), the four-stage fallback chain, _try_structured_retry
          (line 221), _parse_verdict_from_prose (line 151),
          _PROSE_VERDICT_CUES (line 88+), and the in-handle subfailures
          (line 705, 717).
        - workers/agent/treadmill_agent/claude_code.py for the
          claude --print invocation site. The --json-schema flag is
          conditional on the role being the architect; all other
          roles continue without it (their output kinds are different
          — see ctx.role.output_kind switch).
        - workers/agent/treadmill_agent/worker_hints.py for the
          canonical api_client.post_event pattern Task A reuses.

      BUILD:
        1. Define the verdict JSON Schema inline in architecture.py as
           a module-level constant _VERDICT_SCHEMA. FLAT shape — the
           Anthropic tool-schema validator rejects JSON Schema's
           allOf / oneOf / if-then-else (verified 2026-06-07).
           Required top-level fields: verdict (enum), reasoning,
           target_artifact, remediation_summary. Optional:
           rewritten_description, gate_log_excerpt.
        2. claude_code.py::run_claude_code: when ctx.role.id ==
           'role-architect', append --json-schema <inline-schema-json>
           to the claude --print argv. Other roles unchanged.
        3. architecture.py::_extract_verdict_envelope: new shape.
             - Read ctx.claude_result.structured_output (a dict or
               None). If absent, emit task.architect_emit_failure
               (parse_failure_reason='no-structured-output') and
               return a synthetic envelope {"verdict": "emit-failure",
               "model_output_excerpt": <first 2KB of result + summary>}.
             - If present, post-emit-validate in worker code:
                 * verdict in _VALID_VERDICTS (defensive) →
                   parse_failure_reason='invalid-verdict-literal' on miss
                 * if verdict == 'supersede', rewritten_description
                   non-empty → 'supersede-missing-rewrite' on miss
                 * if verdict == 'gate-broken', gate_log_excerpt
                   non-empty → 'gate-broken-missing-excerpt' on miss
               Any check fails → emit failure event, return synthetic
               emit-failure envelope.
             - On success, return structured_output verbatim.
           Emit path uses worker_hints.py's POST pattern: POST to
           /api/v1/tasks/{task_id}/events with entity_type='task',
           action='architect_emit_failure', payload conforms to the
           shared contract block in the parent plan doc.
           ArchitectVerdictParseError stays defined for the line-444
           genuinely-unknown-verdict raise from non-architect call
           sites; never raised from _extract_verdict_envelope.
        4. handle() branch: before the existing accept-as-is / amend
           / supersede / gate-broken switch, recognize verdict ==
           'emit-failure'. Emit StepOutput with decision='emit-failure',
           no dispatch payload, payload carries the model_output_excerpt
           + the parse_failure_reason.
        5. Delete _try_structured_retry, _parse_verdict_from_prose,
           _PROSE_VERDICT_CUES, _RETRY_PROMPT, _find_claude_binary
           in architecture.py. Update _extract_verdict_envelope's
           docstring to name only the structured-output read.

      TEST:
        - workers/agent/tests/test_architect_verdict.py:
          * test_structured_output_present_returns_envelope: stub
            claude_result.structured_output = {verdict:amend, ...},
            assert _extract_verdict_envelope returns it.
          * test_structured_output_absent_emits_failure_no_structured_output:
            stub structured_output=None, assert api_client.post_event
            called with the exact ArchitectEmitFailure shape +
            parse_failure_reason='no-structured-output', synthetic
            envelope returned.
          * test_structured_output_supersede_missing_rewrite_emits_failure:
            stub {verdict:'supersede', rewritten_description:''},
            assert parse_failure_reason='supersede-missing-rewrite'.
          * test_structured_output_gate_broken_missing_excerpt_emits_failure:
            stub {verdict:'gate-broken', gate_log_excerpt:''},
            assert parse_failure_reason='gate-broken-missing-excerpt'.
          * test_structured_output_invalid_verdict_emits_failure: stub
            {verdict:'invalid'}, assert
            parse_failure_reason='invalid-verdict-literal'.
        - workers/agent/tests/test_runner_dispositions_architecture_nothing_to_do.py:
          * test_handle_emit_failure_branch: stub envelope with
            verdict='emit-failure', assert StepOutput shape,
            decision='emit-failure', no dispatch payload.
        - workers/agent/tests/test_claude_code_argv.py (new or
          extended): test_architect_call_appends_json_schema:
          argv assertion that --json-schema appears on architect
          role calls and is absent on other roles.
        - DELETE workers/agent/tests/test_review_disposition_prose_synthesis.py
          (the test file for the prose-cue table). Grep first to
          verify no other tests import _PROSE_VERDICT_CUES or
          _parse_verdict_from_prose.
        - api_client.post_event is mocked in all the above; Task B
          owns the real endpoint.

      DOC: workers/agent/treadmill_agent/runner_dispositions/AGENT.md
      + workers/agent/treadmill_agent/AGENT.md (whichever is the
      closest existing) — Recent-changes entry citing ADR-0083:
      'architect emits via --json-schema; prose-fallback chain
      deleted; emit-failure event POSTed to API; rollback by removing
      the --json-schema flag and reverting _extract_verdict_envelope.'

      Validation MUST NOT call live Anthropic, live AWS, docker, or
      live network. All claude_result fixtures are stubs; the claude
      --print invocation is verified via argv assertion in
      test_claude_code_argv.py, not by invoking the binary.
    scope:
      files:
        - workers/agent/treadmill_agent/runner_dispositions/architecture.py
        - workers/agent/treadmill_agent/claude_code.py
        - workers/agent/tests/test_architect_verdict.py
        - workers/agent/tests/test_runner_dispositions_architecture_nothing_to_do.py
        - workers/agent/tests/test_review_disposition_prose_synthesis.py
        - workers/agent/tests/test_claude_code_argv.py
        - workers/agent/treadmill_agent/runner_dispositions/AGENT.md
        - workers/agent/treadmill_agent/AGENT.md
      services_affected:
        - workers/agent
      out_of_scope:
        - The ArchitectEmitFailure pydantic event payload + the
          relay-drop trigger (Task B)
        - Direct Anthropic API call (sibling ADR / Path B)
        - workflow_runs.cap_exempt column from the prior ADR-0082 plan
        - operator_note re-dispatch trigger from the prior ADR-0082 plan
    validation:
      - kind: deterministic
        description: |
          Worker-side architect-verdict tests pass; existing
          runner-disposition tests stay green; deleted prose-cue
          tests are removed without orphaning imports.
        script: |
          cd workers/agent && uv run pytest tests/test_architect_verdict.py tests/test_runner_dispositions_architecture_nothing_to_do.py tests/test_claude_code_argv.py -q
        severity: blocking
        timeout_seconds: 180
      - kind: llm-judge
        description: |
          AGENT.md Recent-changes entry cites ADR-0083 and names
          the rollback path.
        prompt: |
          The DIFF must include a Recent-changes entry under the
          worker tree's AGENT.md (runner_dispositions/ or sibling)
          that cites ADR-0083, names --json-schema as the wedge,
          and calls out the rollback path. Return 'pass' if present;
          'fail' otherwise.
        severity: blocking

  - id: api-architect-emit-failure-event-and-trigger
    title: "Task B — ArchitectEmitFailure event payload + maybe_drop_relay trigger"
    workflow: wf-author
    intent: |
      STUDY:
        - docs/adrs/0083-architect-verdict-via-json-schema-flag.md
          for the decision shape.
        - The "Shared event-payload contract" section in this plan
          (parent doc) for the exact ArchitectEmitFailure shape Task
          A POSTs.
        - services/api/treadmill_api/events/task.py for the existing
          event payload pattern — the new ArchitectEmitFailure should
          parallel OperatorHintSet (~line 40+) in shape, validation,
          and module-level export.
        - services/api/treadmill_api/coordination/triggers.py for
          the trigger registration pattern. The existing
          maybe_dispatch_* trigger family is the precedent.
        - services/api/treadmill_api/routers/tasks.py for the
          POST /tasks/{id}/events endpoint that Task A will hit.
          Worker emits via this surface; the new event payload
          must be registered as one of the accepted action types.

      BUILD:
        1. services/api/treadmill_api/events/task.py: add
           ArchitectEmitFailure(BaseModel) matching the shared
           contract verbatim. Pydantic Literal['no-structured-output',
           'supersede-missing-rewrite', 'gate-broken-missing-excerpt',
           'invalid-verdict-literal'] for parse_failure_reason. str
           for the other three fields. Mirror OperatorHintSet's
           model_config.
        2. services/api/treadmill_api/routers/tasks.py: register
           'architect_emit_failure' in the action-type dispatch
           switch the POST /tasks/{id}/events endpoint uses
           (whichever pattern that's already in place — examine
           the existing accepted actions; add this one alongside).
           The endpoint must validate the payload against
           ArchitectEmitFailure and persist via the same
           dispatcher.persist_and_publish path other task events
           use.
        3. services/api/treadmill_api/coordination/triggers.py:
           register maybe_drop_relay_on_architect_emit_failure.
           When a task.architect_emit_failure event fires, write a
           markdown file to ~/.cc-channels/<created_by>/relay/
           <ts>-architect-emit-failure-<task_id>.md whose body cites
           the task_id, failing_run_id, parse_failure_reason, the
           model_output_excerpt (truncated to 4KB if longer), and a
           short remediation hint pointing the orchestrator at the
           failing run. The cc-relay channel server on the dispatching
           orchestrator's session host inotify-picks it up.
           For the dev_local deployment where worker and orchestrator
           are on the same machine, a file write is enough. For
           production-split deployments this same code path needs a
           remote-drop mechanism; flag in AGENT.md, out of v1.
        4. The trigger must be idempotent on retry — the relay file
           name carries the failing_run_id so duplicate events
           produce duplicate-but-overwriting drops, not a relay-spam
           cascade. (If the channel server has already consumed the
           file, a duplicate write recreates it; that's acceptable
           in v1 because the channel server's dedup logic is the
           backstop.)

      TEST:
        - services/api/tests/test_architect_emit_failure_event.py
          (new):
          * test_payload_validates: minimal valid payload roundtrips
            through ArchitectEmitFailure; invalid parse_failure_reason
            literal raises ValidationError.
          * test_post_event_persists: POST /tasks/{id}/events with
            action='architect_emit_failure' + valid payload returns
            200 and a task.architect_emit_failure row is persisted.
        - services/api/tests/test_architect_emit_failure_trigger.py
          (new or extension of test_coordination_triggers.py):
          * test_trigger_drops_relay_file: fire a
            task.architect_emit_failure event with created_by='treadmill-bert',
            assert a markdown file appears under
            ~/.cc-channels/treadmill-bert/relay/ matching the
            expected name pattern + carrying the payload fields.
            Use a tmp_path fixture redirecting the relay dir so the
            test doesn't write into the real channel inbox.
          * test_trigger_idempotent_on_replay: fire the same event
            twice, assert the second write does not produce a
            second distinct file (filename is deterministic on
            failing_run_id).

      DOC: services/api/treadmill_api/coordination/AGENT.md (or the
      closest existing parent) — Recent-changes entry citing
      ADR-0083, naming ArchitectEmitFailure + the relay-drop
      trigger + the production-split mechanism as out-of-scope.

      Validation MUST NOT touch live SQS, live AWS, or write into
      the operator's real ~/.cc-channels/. Tests redirect the relay
      dir via env var / tmp_path fixture.
    scope:
      files:
        - services/api/treadmill_api/events/task.py
        - services/api/treadmill_api/routers/tasks.py
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/tests/test_architect_emit_failure_event.py
        - services/api/tests/test_architect_emit_failure_trigger.py
        - services/api/treadmill_api/coordination/AGENT.md
      services_affected:
        - services/api
      out_of_scope:
        - Worker-side disposition + schema threading (Task A)
        - Production-split relay-drop mechanism (deferred — dev_local only in v1)
        - Deterministic detector for stale relay drops (deferred)
    validation:
      - kind: deterministic
        description: |
          New event + trigger tests pass; existing coordination
          tests stay green.
        script: |
          cd services/api && uv run pytest tests/test_architect_emit_failure_event.py tests/test_architect_emit_failure_trigger.py tests/test_coordination_triggers.py -q
        severity: blocking
        timeout_seconds: 180
      - kind: llm-judge
        description: |
          AGENT.md Recent-changes entry cites ADR-0083 and names
          ArchitectEmitFailure + the relay-drop trigger.
        prompt: |
          The DIFF must include a Recent-changes entry under
          services/api/ (closest AGENT.md to the coordination
          module) citing ADR-0083, naming ArchitectEmitFailure and
          the relay-drop trigger, and flagging the production-split
          mechanism as out-of-scope. Return 'pass' if all three
          present; 'fail' otherwise.
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
