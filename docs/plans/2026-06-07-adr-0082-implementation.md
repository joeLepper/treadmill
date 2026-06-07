# Plan: ADR-0082 implementation — architect parse-failure NEEDS_HUMAN escalation

- **Status:** drafting
- **Date:** 2026-06-07
- **Related ADRs:** ADR-0082 (decision, PR #239), ADR-0081 (operator hint channel — load-bearing dependency), ADR-0029 (architect amend cap), ADR-0048 (verdict surface), ADR-0058 (gate-broken)

auto_merge: false

## Goal

Make architect parse-failure escalate to the operator via the ADR-0081 hint channel without consuming an architect amend attempt. When `_extract_verdict_envelope`'s four-stage chain exhausts on a malformed architect summary, the disposition emits a `needs-human` synthetic envelope, an `architect_parse_failure` event fires, and the operator gets notified. The operator sets a one-line `operator_note` with the intended verdict; a trigger re-dispatches the architect step with the note injected into the system prompt. The new architect run's `WorkflowRun` row is marked `cap_exempt=true`, so the amend-cap counter is unchanged from before the parse failure.

## Success criteria

1. `workers/agent/treadmill_agent/runner_dispositions/architecture.py::_extract_verdict_envelope` does NOT raise `ArchitectVerdictParseError` on stage-4 exhaustion. Returns `{"verdict": "needs-human", "reasoning": "<raw architect prose preserved>", "parse_failure": true}`.
2. The same applies to the in-`handle()` mid-parse failures (`supersede` without `rewritten_description`; `gate-broken` without `gate_log_excerpt`) — they reshape as `needs-human` rather than `ArchitectVerdictParseError`.
3. `handle()` recognizes `verdict="needs-human"` and emits a `StepOutput` with `decision="needs-human"` and no downstream dispatch (no `wf-plan` / `wf-feedback` fired).
4. A new `task.architect_parse_failure` event is emitted alongside the existing `task.operator_hint_requested` flow — payload carries the raw prose excerpt + the task_id + the WorkflowRun id of the failing architect run.
5. `workflow_runs.cap_exempt: bool` (default `false`, not nullable) added via Alembic migration; the runnable-rule gate (ADR-0080) is now load-bearing on this PR.
6. `services/api/treadmill_api/coordination/triggers.py::_is_capped` excludes `cap_exempt=true` rows in its `count(WorkflowRun.id)` query.
7. New trigger consumes `task.operator_hint_set` events. When the most recent `wf-architecture-resolve` step on the same task had `decision="needs-human"`, dispatch a fresh `wf-architecture-resolve` run with `cap_exempt=true` set on the new row.
8. Deterministic detector (per ADR-0082 Bad/trade-offs): a scheduled sweep flags tasks in `needs-human` for >24h and emits an alert event.
9. AGENT.md entries land for: `workers/agent/treadmill_agent/runner_dispositions/AGENT.md`, `services/api/treadmill_api/coordination/AGENT.md` (or the closest existing parent), and `services/api/alembic/versions/AGENT.md` (per the migration). Per the `docs-current-with-pr` gate.

## Constraints / scope

### In scope

- Worker-side: `runner_dispositions/architecture.py` reshape of parse-failure → `needs-human` synthetic envelope; tests.
- API-side: `workflow_runs.cap_exempt` column + migration; `_is_capped` exclusion; trigger on `operator_hint_set` re-dispatch; `task.architect_parse_failure` event shape.
- Deterministic detector: scheduled sweep on `needs-human` task age.
- Tests: parse-failure → `needs-human` round-trip, cap counter ignores `cap_exempt`, trigger re-dispatch, operator-note read-at-entry injection.
- AGENT.md updates per the touched components.

### Out of scope

- **Architect prompt hardening / tool-use forced shape.** Donna's vector 3, deferred to a sibling ADR. Reduces fallback-chain hit rate; orthogonal to classification semantics.
- **Backfill of existing capped tasks.** Tasks already in `cap_reached` from prior parse-failures stay there; operator clears them via `--force-bypass-cap` as today. The fix is forward-looking.
- **Dashboard surface for `architect_parse_failure` event rate.** Listed as a follow-up in the ADR; out of v1 — the detector + alert is enough to surface a regression.
- **Cap_exempt for other failure shapes** (terminal_step_failure for non-architect-parse reasons, gate-broken with empty gate_log_excerpt post-prose-fallback, etc.). Scoped tightly to the parse-failure class so the change is auditable.

### Budget

One PR per task, four tasks total. Two worker-side (A, D), two API-side (B, C). Estimated 0.5 day per task. `auto_merge: false` on the plan frontmatter — the Alembic migration on `workflow_runs` is a shared-schema change per the `feedback_auto_merge_frontmatter_omit_unless_blast_radius` rule, so we want operator-merge on each task.

## Sequence of work

```yaml
sequence_of_work:
  - id: parse-failure-needs-human-disposition
    title: "Task A — _extract_verdict_envelope returns needs-human synthetic envelope; handle() routes it"
    workflow: wf-author
    intent: |
      STUDY:
        - workers/agent/treadmill_agent/runner_dispositions/architecture.py
          — _extract_verdict_envelope (line 315), handle() (line 596).
          The four-stage chain (strict JSON → structured retry →
          prose cues → ArchitectVerdictParseError) is intact; only
          stage-4 SEMANTICS change.
        - The two mid-parse raises inside handle(): supersede without
          rewritten_description (line 708), gate-broken without
          gate_log_excerpt (line 725). Both reshape the same way.
        - workers/agent/treadmill_agent/runner_dispositions/AGENT.md
          for the docs-current-with-pr update.
        - docs/adrs/0082-architect-parse-failure-needs-human-escalation.md
          for the decision shape.

      BUILD:
        1. _extract_verdict_envelope: replace the final `raise
           ArchitectVerdictParseError(...)` with `return
           {"verdict": "needs-human", "reasoning": <raw summary
           truncated to 2KB>, "parse_failure": true,
           "parse_failure_reason": "no-verdict-block-no-cue-match"}`.
        2. handle(): branch on verdict == "needs-human" BEFORE the
           existing accept-as-is / amend / supersede / gate-broken
           switch. Emit StepOutput with decision="needs-human", no
           dispatch payload, payload carries the parse_failure_reason
           + raw prose excerpt + the architect role's model name so
           the API-side trigger has provenance to attach to the
           emitted event.
        3. The two mid-parse raises (supersede-no-rewrite,
           gate-broken-no-excerpt) become returns of the same
           needs-human envelope shape with parse_failure_reason set
           to "supersede-missing-rewritten-description" and
           "gate-broken-missing-log-excerpt" respectively.
        4. ArchitectVerdictParseError class stays defined (other call
           sites may still raise it for genuinely unknown verdict
           strings — line 444); it just is not raised from the
           four-stage exhaustion or the two in-handle subfailures.

      TEST:
        - workers/agent/tests/test_architect_verdict.py (if it exists;
          otherwise sibling): three new cases —
          * test_parse_failure_returns_needs_human_envelope: stage-4
            exhaustion → envelope shape, no exception.
          * test_supersede_missing_rewrite_returns_needs_human:
            handle()'s mid-parse subfailure → needs-human envelope.
          * test_gate_broken_missing_excerpt_returns_needs_human:
            same.
        - workers/agent/tests/test_runner_dispositions_architecture_*.py
          (whichever covers the handle() routing): one new case for
          decision="needs-human" branch — StepOutput shape, no
          dispatch payload, correct parse_failure_reason.

      DOC: workers/agent/treadmill_agent/runner_dispositions/AGENT.md —
      Recent-changes entry citing ADR-0082, naming the needs-human
      verdict variant + the three parse_failure_reason values.

      Validation MUST NOT call live AWS, docker, or the real Claude
      CLI subprocess. The structured-output retry path is stubbed in
      tests by monkeypatching _try_structured_retry to return None.
    scope:
      files:
        - workers/agent/treadmill_agent/runner_dispositions/architecture.py
        - workers/agent/tests/test_architect_verdict.py
        - workers/agent/tests/test_runner_dispositions_architecture_nothing_to_do.py
        - workers/agent/treadmill_agent/runner_dispositions/AGENT.md
      services_affected:
        - workers/agent
      out_of_scope:
        - The API-side cap_exempt column (Task B owns it)
        - The trigger on operator_hint_set (Task C owns it)
        - Prompt hardening
    validation:
      - kind: deterministic
        description: |
          New parse-failure tests pass; existing architect tests stay
          green; pytest discovery is clean.
        script: |
          cd workers/agent && uv run pytest tests/test_architect_verdict.py tests/test_runner_dispositions_architecture_nothing_to_do.py tests/test_review_disposition_prose_synthesis.py -q
        severity: blocking
        timeout_seconds: 180
      - kind: llm-judge
        description: |
          AGENT.md Recent-changes entry under
          workers/agent/treadmill_agent/runner_dispositions/AGENT.md
          cites ADR-0082, names needs-human as the new verdict
          variant, and lists the three parse_failure_reason values.
        prompt: |
          The DIFF must include a Recent-changes entry that mentions
          ADR-0082, the needs-human synthetic envelope, and the three
          parse_failure_reason cues (no-verdict-block-no-cue-match,
          supersede-missing-rewritten-description,
          gate-broken-missing-log-excerpt). Return 'pass' if all three
          present; 'fail' otherwise.
        severity: blocking

  - id: workflow-runs-cap-exempt-column
    title: "Task B — workflow_runs.cap_exempt column + migration + _is_capped exclusion + emits architect_parse_failure event"
    workflow: wf-author
    intent: |
      STUDY:
        - services/api/treadmill_api/models/run.py — WorkflowRun
          schema (currently no outcome column).
        - services/api/treadmill_api/coordination/triggers.py — the
          _is_capped function (~line 419) and the
          maybe_dispatch_terminal_step_failure_escalation trigger
          (~line 2461). The terminal_step_failure escalation is
          where parse-failure currently lands; we want the new
          architect_parse_failure event emitted from the same path
          BEFORE the terminal escalation fires (so the operator
          sees both signals during transition).
        - services/api/alembic/versions/ — pattern for adding a
          non-null bool column with default false; recent precedent
          is the ADR-0076 RepoConfig git_author_* migration
          (2026-06-05). Use op.create_check_constraint(name,
          table_name, condition) — argument order matters (caught
          by ADR-0080's runnable-rule gate, which is now load-bearing
          on this PR).
        - services/api/treadmill_api/events/task.py — shape new
          ArchitectParseFailure event there.

      BUILD:
        1. Alembic migration adding cap_exempt BOOL NOT NULL DEFAULT
           FALSE to workflow_runs. Down migration drops it.
        2. WorkflowRun model: add `cap_exempt: Mapped[bool] =
           mapped_column(Boolean, nullable=False, server_default=
           "false")` with a class-level docstring citing ADR-0082.
        3. _is_capped: add `WorkflowRun.cap_exempt.is_(False)` to the
           WHERE clause. Existing call sites unchanged.
        4. maybe_dispatch_terminal_step_failure_escalation: when the
           failing step's payload carries decision="needs-human"
           (Task A's emit), emit a new
           task.architect_parse_failure event INSTEAD of the existing
           task.escalated_to_operator(reason="terminal_step_failure").
           Payload: parse_failure_reason + raw prose excerpt + the
           failing WorkflowRun id + task_id. The TerminalStepFailure
           escalation does not fire for this case — needs-human IS
           the escalation.
        5. ArchitectParseFailure pydantic event in events/task.py
           with the payload shape above.

      TEST:
        - services/api/tests/test_workflow_runs_cap_exempt.py (new):
          * test_is_capped_excludes_cap_exempt_rows: insert 3 runs
            against a task, mark 1 cap_exempt; _is_capped should see
            count=2, not 3.
          * test_migration_round_trip: alembic upgrade head + downgrade
            back-to-base produces no schema drift (per ADR-0080).
        - services/api/tests/test_terminal_step_failure_escalation.py
          (or sibling): one case showing that a step with
          decision="needs-human" emits ArchitectParseFailure (not
          TerminalStepFailure) and does NOT decrement the cap.

      DOC: services/api/treadmill_api/coordination/AGENT.md (or the
      closest existing parent) — Recent-changes entry citing ADR-0082,
      naming cap_exempt and the new event.

      Validation MUST NOT touch a live DB. Migration round-trip is
      checked via `alembic upgrade --sql head` (offline mode,
      sandbox-safe per ADR-0080) — exit code + DDL keyword
      presence is the gate. _is_capped tests use the existing
      sqlite/stub-session fixture pattern.
    scope:
      files:
        - services/api/alembic/versions/  # new migration file
        - services/api/treadmill_api/models/run.py
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/treadmill_api/events/task.py
        - services/api/tests/test_workflow_runs_cap_exempt.py
        - services/api/tests/test_terminal_step_failure_escalation.py
        - services/api/treadmill_api/coordination/AGENT.md
      services_affected:
        - services/api
      out_of_scope:
        - Worker-side disposition reshape (Task A)
        - Re-dispatch trigger on operator_hint_set (Task C)
    validation:
      - kind: deterministic
        description: |
          New cap_exempt + parse_failure tests pass; existing
          coordination tests stay green; migration round-trips
          through alembic --sql head.
        script: |
          cd services/api && uv run pytest tests/test_workflow_runs_cap_exempt.py tests/test_terminal_step_failure_escalation.py tests/test_coordination_triggers.py -q && bash ../../tools/rule-checks/alembic-migration-runnable/check.sh
        severity: blocking
        timeout_seconds: 240
      - kind: llm-judge
        description: |
          AGENT.md Recent-changes entry cites ADR-0082 and names
          cap_exempt + task.architect_parse_failure event.
        prompt: |
          The DIFF must include a Recent-changes entry in services/api/
          (closest AGENT.md to the coordination module) citing
          ADR-0082, the cap_exempt column, and the
          task.architect_parse_failure event. Return 'pass' if all
          three present; 'fail' otherwise.
        severity: blocking

  - id: operator-note-set-redispatches-architect
    title: "Task C — trigger on task.operator_hint_set re-dispatches wf-architecture-resolve with cap_exempt=true"
    workflow: wf-author
    depends_on:
      - task.parse-failure-needs-human-disposition.pr_merged
      - task.workflow-runs-cap-exempt-column.pr_merged
    intent: |
      STUDY:
        - services/api/treadmill_api/coordination/triggers.py for
          the existing trigger registration pattern (the
          maybe_dispatch_* functions hooked off persist_and_publish).
        - services/api/treadmill_api/events/task.py operator_hint_set
          shape.
        - The Task A / Task B implementation in main — needs-human
          payload shape + cap_exempt column are now load-bearing.

      BUILD:
        1. New trigger maybe_dispatch_architect_redispatch_on_hint
           reacting to task.operator_hint_set events.
        2. Filter: only fire when the task's MOST RECENT
           wf-architecture-resolve WorkflowRun's step decision was
           "needs-human" (left join workflow_runs +
           workflow_run_steps; LIMIT 1 by created_at desc).
        3. On match: create a new WorkflowRun for wf-architecture-
           resolve with cap_exempt=true and source_step_id pointing
           at the prior needs-human step so the audit chain is intact.
        4. Idempotency: the existing dedup window (5 min on
           escalation events per Alan's read at triggers.py:2458)
           applies. We rely on the operator setting one note per
           needs-human episode; if they update the note within the
           window, only the first dispatch fires.

      TEST:
        - services/api/tests/test_architect_redispatch_on_hint.py
          (new):
          * test_redispatches_when_prior_step_is_needs_human: insert
            a needs-human step, fire operator_hint_set, assert a new
            WorkflowRun with cap_exempt=true and a wf-architecture-
            resolve workflow_version_id.
          * test_does_not_redispatch_when_prior_step_is_amend: same
            shape, prior decision="amend" → no new run.
          * test_does_not_double_dispatch_within_dedup_window: two
            operator_hint_set within 5 min → exactly one new run.

      DOC: services/api/treadmill_api/coordination/AGENT.md — extend
      the Task-B Recent-changes entry to include the new trigger.

      Validation MUST NOT touch live SQS or a live DB. Tests use the
      existing stub-session + dispatcher harness.
    scope:
      files:
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/tests/test_architect_redispatch_on_hint.py
        - services/api/treadmill_api/coordination/AGENT.md
      services_affected:
        - services/api
      out_of_scope:
        - The architect prompt itself — Task A's worker disposition
          already reads operator_note via the ADR-0081 read-at-entry
          path; no prompt change needed.
    validation:
      - kind: deterministic
        description: |
          New redispatch tests pass; existing trigger tests stay
          green.
        script: |
          cd services/api && uv run pytest tests/test_architect_redispatch_on_hint.py tests/test_coordination_triggers.py -q
        severity: blocking
        timeout_seconds: 180

  - id: needs-human-age-detector
    title: "Task D — deterministic detector emits stale-needs-human alert at >24h age"
    workflow: wf-author
    depends_on:
      - task.parse-failure-needs-human-disposition.pr_merged
      - task.workflow-runs-cap-exempt-column.pr_merged
    intent: |
      STUDY:
        - Existing scheduled-sweep pattern (the health-bot family per
          memory project_health_bots_track — P1+P2 deployed; this is
          a new P-tier detector following the same shape).
        - services/api/treadmill_api/seed/schedules.py for the
          schedule registration shape.

      BUILD:
        1. New sweep function detect_stale_needs_human_tasks that:
           - Queries workflow_run_steps where decision="needs-human"
             AND created_at > now() - interval '24 hours' (to bound
             the scan) AND task is not in a terminal status.
           - Cross-checks task.operator_note: if still null, the
             task is genuinely stalled.
           - Emits task.architect_parse_failure_stale event per
             affected task once per 24h-window (idempotency on
             event dedup).
        2. Schedule entry running every 4 hours (cheap query, no
           need for tighter cadence — operator typically sees the
           initial architect_parse_failure event in ADR-0081's
           notification surface much sooner).

      TEST:
        - services/api/tests/test_needs_human_age_detector.py (new):
          * test_emits_alert_for_stale_needs_human_task: insert a
            needs-human step at -25h, operator_note null, run sweep,
            assert event emitted.
          * test_does_not_alert_when_operator_note_set: same shape,
            operator_note non-null → no event.
          * test_does_not_double_alert_within_window: two sweep runs
            within 24h → exactly one event.

      DOC: services/api/treadmill_api/coordination/AGENT.md (or
      seed/schedules.py's parent) — Recent-changes entry citing
      ADR-0082's Bad/trade-offs mitigation.

      Validation MUST NOT touch live SQS or DB. Tests use freeze-time
      style or pinning created_at on inserted rows.
    scope:
      files:
        - services/api/treadmill_api/coordination/  # new detector file
        - services/api/treadmill_api/seed/schedules.py
        - services/api/tests/test_needs_human_age_detector.py
        - services/api/treadmill_api/coordination/AGENT.md
      services_affected:
        - services/api
      out_of_scope:
        - Dashboard surface for the alert (ADR follow-up)
    validation:
      - kind: deterministic
        description: |
          Detector tests pass; existing sweep tests stay green.
        script: |
          cd services/api && uv run pytest tests/test_needs_human_age_detector.py tests/test_scheduled_sweeps.py -q
        severity: blocking
        timeout_seconds: 180
```

## Risks / unknowns

- **`wf-operator-note-await` workflow ID.** ADR-0082's prose mentions it as the routing destination, but Task A's StepOutput emits `decision="needs-human"` with no dispatch payload — the task is paused via the absence of a downstream workflow, and the re-dispatch is trigger-driven (Task C). We do NOT create a new workflow_id; we use the disposition + trigger pair. If review surfaces a need for an explicit await-workflow row in the workflows table (audit trail visibility), fold it into Task C scope.
- **`workflow_runs.cap_exempt` migration safety.** `workflow_runs` is a hot table; adding a NOT NULL column with a constant default is online-safe on Postgres 11+, but worth flagging during ADR-0080 gate review. Mitigation: the migration uses `server_default=text('false')` so existing rows backfill without a separate UPDATE.
- **Idempotency on operator_hint_set re-dispatch.** If the operator updates the note multiple times in quick succession (corrections), only the first triggers a re-dispatch within the 5-min dedup window. Acceptable for v1; a later refinement might use the architect-attempt-since-last-needs-human counter instead of time-window dedup.
- **Backfill of capped tasks not in scope.** Operators clearing existing `cap_reached` tasks today via `--force-bypass-cap` continue to do so. The fix is forward-looking; if Joe wants a one-shot backfill (manually mark prior parse-failure runs as `cap_exempt=true` and re-evaluate `_is_capped`), it's a follow-up SQL operation, not a code change.

## Decisions captured during execution

_Empty._

## Post-mortem

_Filled on completion._
