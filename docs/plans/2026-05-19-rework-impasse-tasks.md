# Plan: rework five spec-impasse tasks

Status: **drafting**
Replaces: tasks `02789bf6`, `80f5bed5`, `8dce5394`, `9b81e083`, `e7ffc11e` (the 5 impasses observed live 2026-05-19) **plus** `9bb999ca`, `20563fc2`, `1f920ed7` (3 downstream tasks that depend on the originals by UUID and would never unblock even if the originals merged). Each was either stuck in the architect→feedback recovery loop or dep-blocked on a task that was.

## Why this plan exists

The 2026-05-19 retry batch (post-PR-#198/#199 deploy) ran the architect-as-recoverer loop end-to-end on five stuck wf-feedback tasks. All five engaged architect arbitration (PR #198's trigger fired); all five burned two architect attempts emitting `amend`; none produced a PR. Diagnosis from reading the specs:

1. **Validation scripts run the WHOLE existing test suite.** When `pytest tests/test_starters.py -q` fires and seven pre-existing unrelated tests fail (e.g., `treadmill_cli` import errors), the whole validation reports fail through no fault of the worker's diff.
2. **Multi-file coherence in one attempt.** Specs require 4+ files of related code (production + migration + event class + integration test) to be authored coherently in a single attempt, with no incremental signal.
3. **Exact-string grep gates.** Specs encode "the literal X must appear in file Y" but the worker doesn't always know which spelling will satisfy the grep.
4. **References to historical context.** "tasks 209f4d8e and 0739cee3 on 2026-05-16" or "lines 99–116 in normalize.py" — the worker can't easily look these up.
5. **Validation tests don't exist yet.** Each spec's validation script runs an integration test file that the worker is expected to CREATE as part of the task. Two artifacts (the implementation and the test) must both be correct on first try.

The replacements below address each anti-pattern:
- Validation scripts run **only the new test file** plus a minimal literal-string grep.
- Big specs are **split into sequential dependent sub-tasks**.
- Descriptions are **self-contained** — file paths, function names, and string literals are explicit; no references to historical task IDs or line numbers.
- Each task ships **one feature** worth of code (one role definition, one migration, one event class — not all four at once).

## What we keep, drop, or split per stuck task

- `80f5bed5` → **keep as one task, narrower validation.** Spec was already focused; the validation just needed to stop running the whole `test_starters.py`.
- `8dce5394` → **split into 3.** Original required new role + new workflow + new trigger; split into role-definition, workflow-definition, trigger-wiring.
- `02789bf6` → **split into 2.** Original bundled "fix silent-fail misread in VIEW" and "add pr_merged reconciler" — independent fixes, hand them out separately.
- `9b81e083` → **keep as one task with tighter scope.** Original spec was already small but the validation ran the whole suite + required a migration.
- `e7ffc11e` → **keep as one task with narrower validation.** Same shape as 9b81e083.

Net: 5 original impasse tasks + 3 dep-blocked downstreams → **11 replacement tasks**, each smaller and more achievable. The 3 downstream replacements re-establish their dependencies as intra-plan slug references (e.g. `task.role-documentarian-tune-rule-prompt.pr_merged`) rather than the dangling UUID references that today point at the stuck originals.

## Operator action before submitting

Cancel or let cap out the five in-flight stuck task runs so they stop burning architect attempts. The DB-level cancel is one statement per task, or just let them run — they'll hit the 5-attempt cap and surface to operator naturally.

## sequence_of_work

```yaml
sequence_of_work:
  - id: role-documentarian-tune-rule-prompt
    title: role-documentarian system_prompt handles tune-rule-from-architect intent
    workflow: wf-author
    intent: |
      Edit ``services/api/treadmill_api/starters.py``. Find the role with
      ``id="role-documentarian"`` (search for that literal). In its
      ``system_prompt`` field (a multi-line string), append a section that
      tells the role how to handle the new intent literal
      ``tune-rule-from-architect``. The section MUST mention these three
      action literals verbatim, each on its own line or sentence:
        - ``demote_severity``
        - ``narrow_applies_to``
        - ``refine_prompt``
      For each action, give a one-sentence pattern saying what gets edited:
      ``demote_severity`` → ``checks[i].severity`` in the rule YAML;
      ``narrow_applies_to`` → ``applies_to`` list in the rule YAML;
      ``refine_prompt`` → ``checks[i].prompt`` text in the rule YAML.
      The rule YAML lives at ``docs/knowledge-base/rules/<rule-slug>.yaml``;
      mention this path.

      Then create a NEW test file at
      ``services/api/tests/test_starters_role_documentarian_tune_rule.py``
      with one test that:
        - imports the role-documentarian role from starters.py (the
          starters dict is at module level)
        - asserts the role's ``system_prompt`` contains the literal
          ``"tune-rule-from-architect"``
        - asserts the prompt contains each of the three action literals
          (``demote_severity``, ``narrow_applies_to``, ``refine_prompt``)
        - asserts the prompt mentions
          ``docs/knowledge-base/rules/`` as a path
      Use plain assertions; don't over-engineer.
    scope:
      files:
        - services/api/treadmill_api/starters.py
        - services/api/tests/test_starters_role_documentarian_tune_rule.py
    validation:
      - kind: deterministic
        description: |
          The new test file passes and the prompt contains the required
          literal. No other test suites are touched.
        script: |
          cd services/api \
            && grep -q "tune-rule-from-architect" treadmill_api/starters.py \
            && uv run pytest tests/test_starters_role_documentarian_tune_rule.py -q

  - id: role-rule-corpus-auditor-definition
    title: Define role-rule-corpus-auditor in starters.py
    workflow: wf-author
    intent: |
      Add a new role to the starters dict in
      ``services/api/treadmill_api/starters.py``. Use the existing role
      entries as a template (e.g., search for ``"id": "role-reviewer"``
      and pattern-match the structure).

      Role spec:
        id: role-rule-corpus-auditor
        model: claude-haiku-4-5-20251001
        output_kind: ANALYSIS (look up the existing OutputKind enum
          import and use the analysis variant — match how role-feedback
          and role-ci-analyzer do it)
        system_prompt: a multi-line string that instructs the role to:
          - audit the rule corpus at
            ``docs/knowledge-base/rules/*.yaml``
          - identify rules whose ``severity`` is wrong relative to their
            ``applies_to`` selector breadth
          - identify rules whose ``checks[i].prompt`` has drifted from
            current code patterns
          - emit a JSON envelope (per ADR-0027) with a list of proposed
            deprecate/update changes, each entry naming the rule slug
            and a one-line rationale
      The prompt must end with the instruction to emit the JSON envelope
      and nothing else.

      Then create a NEW test file at
      ``services/api/tests/test_starters_role_rule_corpus_auditor.py``
      with one test that asserts the role exists in the starters dict
      and its ``system_prompt`` contains the literal "rule corpus".
    scope:
      files:
        - services/api/treadmill_api/starters.py
        - services/api/tests/test_starters_role_rule_corpus_auditor.py
    validation:
      - kind: deterministic
        description: |
          The new role is defined and the small targeted test passes.
        script: |
          cd services/api \
            && grep -q '"id": "role-rule-corpus-auditor"' treadmill_api/starters.py \
            && uv run pytest tests/test_starters_role_rule_corpus_auditor.py -q

  - id: wf-rule-corpus-health-workflow
    title: Define wf-rule-corpus-health workflow in starters.py
    workflow: wf-author
    depends_on:
      - task.role-rule-corpus-auditor-definition.pr_merged
    intent: |
      Add a new workflow to the starters dict in
      ``services/api/treadmill_api/starters.py``. Use existing workflows
      (e.g., search for ``"id": "wf-feedback"``) as templates.

      Workflow spec:
        id: wf-rule-corpus-health
        steps:
          - step 1: role-rule-corpus-auditor (analyzer)
          - step 2: role-code-author (action) — the same role used by
            wf-feedback and wf-author for code editing
      The exact dict structure should match how existing two-step
      workflows are defined (analyzer step first, action step second,
      both with step_name and role_id fields). Look at wf-feedback's
      definition for the shape.

      Then create a NEW test file at
      ``services/api/tests/test_starters_wf_rule_corpus_health.py``
      with one test asserting the workflow exists in the starters dict
      and has exactly two steps with the expected role IDs.
    scope:
      files:
        - services/api/treadmill_api/starters.py
        - services/api/tests/test_starters_wf_rule_corpus_health.py
    validation:
      - kind: deterministic
        description: |
          The new workflow is defined and the small targeted test passes.
        script: |
          cd services/api \
            && grep -q '"id": "wf-rule-corpus-health"' treadmill_api/starters.py \
            && uv run pytest tests/test_starters_wf_rule_corpus_health.py -q

  - id: wf-rule-corpus-health-trigger
    title: Route scheduled-tick events to wf-rule-corpus-health
    workflow: wf-author
    depends_on:
      - task.wf-rule-corpus-health-workflow.pr_merged
    intent: |
      Edit ``services/api/treadmill_api/coordination/triggers.py`` to add
      a new trigger function ``maybe_dispatch_rule_corpus_health_on_tick``.
      Pattern-match against the existing trigger functions in the same
      file (search for ``async def maybe_dispatch_`` to find examples;
      ``maybe_dispatch_feedback_on_step_failed`` is a good shape to copy).

      Predicate:
        - the incoming event is ``ScheduledTick`` (entity_type='schedule',
          action='tick' — verify by looking at how
          maybe_dispatch_feedback_on_step_failed handles its predicate)
        - the schedule's ``workflow_id`` field equals
          ``"wf-rule-corpus-health"``
      Behavior:
        - resolve the task associated with this schedule (look at
          existing scheduled-tick handlers for the pattern; if none
          exists, create a placeholder task per the schedule's
          ``task_template`` or whatever field is used)
        - dispatch wf-rule-corpus-health via
          ``_create_and_publish_run``
        - dedup key:
          ``wf-rule-corpus-health:<repo>:scheduled-tick=<schedule_id>:<tick_id>``

      Wire the new helper into the consumer at
      ``services/api/treadmill_api/coordination/consumer.py``: find the
      handler that processes ``schedule.tick`` events and add a call to
      ``maybe_dispatch_rule_corpus_health_on_tick`` alongside any
      existing tick handlers.

      Then create a NEW test file at
      ``services/api/tests/test_triggers_rule_corpus_health_tick.py``
      with two tests:
        1. tick fires with matching workflow_id → wf-rule-corpus-health
           dispatched
        2. tick fires with a different workflow_id → not dispatched
    scope:
      files:
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/treadmill_api/coordination/consumer.py
        - services/api/tests/test_triggers_rule_corpus_health_tick.py
    validation:
      - kind: deterministic
        description: |
          The new trigger function exists and the targeted tests pass.
        script: |
          cd services/api \
            && grep -q "maybe_dispatch_rule_corpus_health_on_tick" treadmill_api/coordination/triggers.py \
            && uv run pytest tests/test_triggers_rule_corpus_health_tick.py -q

  - id: task-status-view-silent-fail
    title: Distinguish silently-failed from done in task_status VIEW
    workflow: wf-author
    intent: |
      The ``task_status`` SQL VIEW currently classifies tasks as ``done``
      in clause 6d when the latest wf-author step.completed has no
      validation_results indicating failure. We've observed cases where
      wf-author completed without producing a PR — silent failure — but
      the VIEW still reports ``done``.

      Add a new derived_status value: ``silently-failed``. Update
      clause 6d to inspect the latest step's
      ``output.payload.validation_results`` (a JSONB array). If any
      entry has ``verdict='fail'``, the derived_status is
      ``silently-failed`` rather than ``done``.

      Find the VIEW definition — search the alembic migrations under
      ``services/api/alembic/versions/`` for files that create or alter
      the task_status VIEW. Add a new migration (datetime-keyed revision
      id per ADR-0044; look at the most recent migration for the format)
      that drops and recreates the VIEW with the new clause.

      Then create a NEW test file at
      ``services/api/tests/test_task_status_view_silent_fail.py`` with
      one integration test that:
        - creates a task with a wf-author run whose final step.completed
          has output.payload.validation_results = [{"verdict": "fail",
          ...}]
        - asserts the task's derived_status in task_status is
          ``silently-failed``
        - also asserts a control case: a task with all validations
          passing reports derived_status='done' (regression).
    scope:
      files:
        - services/api/alembic/versions/<new-migration-file>
        - services/api/tests/test_task_status_view_silent_fail.py
    validation:
      - kind: deterministic
        description: |
          The migration applies cleanly, the VIEW recognizes the new
          state, and the targeted test passes.
        script: |
          cd services/api \
            && uv run alembic upgrade head \
            && uv run pytest tests/test_task_status_view_silent_fail.py -q

  - id: pr-merged-reconciler-on-event
    title: Reconcile task_status on pr_merged when task_prs row absent
    workflow: wf-author
    depends_on:
      - task.task-status-view-silent-fail.pr_merged
    intent: |
      When a PR is merged on a Treadmill-authored branch but no
      ``task_prs`` row was ever inserted (because the branch was
      operator-completed, or a prior wf-author crashed before writing
      the row), the task's derived_status sticks at ``pr_opened`` even
      though the PR is merged on GitHub.

      Add a reconciler helper in
      ``services/api/treadmill_api/coordination/triggers.py`` called
      ``maybe_reconcile_task_prs_on_pr_merged``. It runs on every
      ``github.pr_merged`` event and:
        - parses the PR's head.ref (branch name) from the event payload
        - looks up tasks whose authored branch matches that ref (the
          branch-naming convention is ``task/<task_id>-<title-slug>`` —
          extract the task_id prefix)
        - if a matching task exists and task_prs has NO row for this
          (repo, pr_number), INSERT one with the merged_at timestamp
          from the event
      This deterministically reconciles operator-completed PRs into the
      DB so task_status projects ``derived_status='done'``.

      Wire the helper into consumer.py's pr_merged event handler
      (search for ``maybe_emit_review_override_on_architect_completion``
      or similar to find where pr_merged events are processed).

      Then create a NEW test file at
      ``services/api/tests/test_pr_merged_reconciler.py`` with two tests:
        1. pr_merged event with no prior task_prs row, branch matches a
           task → row is inserted, derived_status flips to done
        2. pr_merged event for an existing task_prs row → no duplicate
           insert (idempotent)
    scope:
      files:
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/treadmill_api/coordination/consumer.py
        - services/api/tests/test_pr_merged_reconciler.py
    validation:
      - kind: deterministic
        description: |
          The reconciler runs and the targeted tests pass.
        script: |
          cd services/api \
            && grep -q "maybe_reconcile_task_prs_on_pr_merged" treadmill_api/coordination/triggers.py \
            && uv run pytest tests/test_pr_merged_reconciler.py -q

  - id: pr-closed-event-and-projection
    title: pr_closed event verb + task_status projection
    workflow: wf-author
    intent: |
      The webhook normalizer at
      ``services/api/treadmill_api/webhooks/normalize.py`` currently
      emits ``pr_merged`` for ``action="closed"`` with
      ``merged=True``. PRs closed without merge are silently dropped.
      Add coverage for closed-without-merge:

      Steps:
        1. Add a new event class ``GithubPrClosed`` in
           ``services/api/treadmill_api/events/github.py``. Pattern-match
           ``GithubPrMerged`` (in the same file). Fields:
           ``repo: str``, ``pr_number: int``, ``sender: str``,
           ``head_branch: str | None``.
        2. Register it in ``treadmill_api/events/registry.py`` (add to
           ``_REGISTRY_CLASSES``) and re-export from
           ``events/__init__.py`` (add to imports + __all__).
        3. In ``normalize.py``, find the branch that handles
           ``action="closed"`` and emit ``pr_closed`` events when
           ``merged=False`` (in addition to the existing ``pr_merged``
           emission for ``merged=True``).
        4. Add a derived_status value ``pr_closed_without_merge``.
           Update the task_status VIEW (new alembic migration,
           datetime-keyed per ADR-0044) so a task whose latest event for
           this PR is ``github.pr_closed`` projects this new status.

      Then create a NEW test file at
      ``services/api/tests/test_pr_closed_event.py`` with:
        - a unit test that the normalizer emits ``pr_closed`` for
          ``action="closed", merged=False``
        - an integration test that derived_status becomes
          ``pr_closed_without_merge`` after the event lands.
    scope:
      files:
        - services/api/treadmill_api/events/github.py
        - services/api/treadmill_api/events/registry.py
        - services/api/treadmill_api/events/__init__.py
        - services/api/treadmill_api/webhooks/normalize.py
        - services/api/alembic/versions/<new-migration-file>
        - services/api/tests/test_pr_closed_event.py
    validation:
      - kind: deterministic
        description: |
          All four code changes ship, the migration applies, and the
          targeted tests pass.
        script: |
          cd services/api \
            && grep -q "class GithubPrClosed" treadmill_api/events/github.py \
            && uv run alembic upgrade head \
            && uv run pytest tests/test_pr_closed_event.py -q

  - id: tuning-pr-opts-out-of-auto-merge
    title: Tune-rule PRs land with auto_merge=false
    workflow: wf-author
    depends_on:
      - task.role-documentarian-tune-rule-prompt.pr_merged
    intent: |
      When ``role-documentarian`` produces a PR for the
      ``tune-rule-from-architect`` intent, the resulting ``task_prs``
      row's ``auto_merge`` column must be ``false`` so the auto-merge
      coordinator does not auto-merge these tuning PRs.

      Edit ``services/api/treadmill_api/coordination/triggers.py``.
      Find the helper that dispatches wf-doc-amend for tuning intents
      (search for ``maybe_dispatch_rule_tuning_on_architect_completion``;
      the function name may differ — look for the existing
      ``tune-rule-from-architect`` literal handling). When inserting the
      ``task_prs`` row for the resulting PR, set ``auto_merge=False``.

      If the ``task_prs`` model doesn't yet have an ``auto_merge``
      column, check ``services/api/treadmill_api/models/task_prs.py``
      (or the file that defines the ``task_prs`` SQLAlchemy model).
      Adding the column is OUT OF SCOPE for this task — if it doesn't
      exist, set the flag via the existing column conventions used by
      the auto_merge coordinator (search the coordinator for how it
      reads auto_merge per-task; pattern-match).

      Then create a NEW test file at
      ``services/api/tests/test_tuning_pr_auto_merge_opt_out.py``
      with one test:
        - register a synthetic architect step.completed with verdict
          ``accept-as-is`` + ``validator_tuning`` payload
        - trigger the tune-rule dispatch
        - assert the resulting ``task_prs`` row's auto_merge flag is
          false
      Use the in-memory or fixture-based test patterns from
      ``test_supersede_trigger.py`` or
      ``test_triggers_architect_amend_remediation_plumbing.py`` —
      pattern-match either.
    scope:
      files:
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/tests/test_tuning_pr_auto_merge_opt_out.py
    validation:
      - kind: deterministic
        description: |
          The targeted test passes.
        script: |
          cd services/api \
            && uv run pytest tests/test_tuning_pr_auto_merge_opt_out.py -q

  - id: replay-pr-merged-cli
    title: treadmill replay pr_merged CLI subcommand
    workflow: wf-author
    depends_on:
      - task.pr-closed-event-and-projection.pr_merged
    intent: |
      Add a new CLI subcommand: ``treadmill replay --event-type
      pr_merged --since <sha>``. The subcommand re-projects historic
      ``github.pr_merged`` events against current VIEWs so that newly
      added projections (e.g. from migrations) populate against past
      events.

      Implementation:
        - Add the subcommand definition in ``cli/treadmill_cli/cli.py``.
          Pattern-match against the existing subcommands (search for
          ``@app.command(`` to see how others are structured).
        - The subcommand POSTs to a new API endpoint
          ``POST /api/v1/events/replay`` with body
          ``{"event_type": "pr_merged", "since": "<sha>"}``.
        - Add the endpoint in
          ``services/api/treadmill_api/routers/`` (create a new
          ``events.py`` router or use an existing one; pattern-match
          the existing routers' shape).
        - The endpoint queries the ``events`` table for matching
          ``entity_type='github'``, ``action='pr_merged'`` rows since
          the SHA-anchored timestamp, then re-publishes each via the
          existing event-publish path so the VIEW projections re-run.

      Then create a NEW test file at
      ``cli/tests/test_replay_command.py`` with two tests:
        1. CLI accepts ``--event-type pr_merged --since <sha>`` and
           POSTs the expected JSON body.
        2. CLI rejects unsupported event-types with a clear error
           (use whatever pytest helpers the existing CLI tests use for
           Typer CliRunner-style invocation; pattern-match
           ``cli/tests/test_cli.py`` if it exists).
      Create a NEW test file for the API endpoint at
      ``services/api/tests/test_events_replay_endpoint.py`` that
      asserts it accepts the body, queries the events table correctly,
      and returns a 200 with a count of replayed events.
    scope:
      files:
        - cli/treadmill_cli/cli.py
        - services/api/treadmill_api/routers/events.py
        - cli/tests/test_replay_command.py
        - services/api/tests/test_events_replay_endpoint.py
    validation:
      - kind: deterministic
        description: |
          Both targeted test files pass.
        script: |
          cd cli && uv run pytest tests/test_replay_command.py -q \
            && cd ../services/api && uv run pytest tests/test_events_replay_endpoint.py -q

  - id: seed-periodic-rule-corpus-health-schedule
    title: Add periodic-rule-corpus-health to schedules.py seed
    workflow: wf-author
    depends_on:
      - task.wf-rule-corpus-health-trigger.pr_merged
    intent: |
      Add a new seed entry to
      ``services/api/treadmill_api/seed/schedules.py``. Pattern-match
      against the existing entries in the same file.

      The new entry's fields (verbatim):
        - id: ``periodic-rule-corpus-health``
        - cron: ``0 21 * * 0``
        - workflow_id: ``wf-rule-corpus-health``
        - quiet_hours: ``null``
        - quiet_tz: ``America/Los_Angeles``
        - jitter_seconds: ``60``
        - payload_template: ``{"trigger": "scheduled-sweep"}``

      The seed must be idempotent (re-running ``treadmill schedules
      seed`` produces no duplicate rows). The existing seed code likely
      already handles idempotency via UPSERT or
      ``ON CONFLICT DO NOTHING``; pattern-match.

      Then create a NEW test file at
      ``services/api/tests/test_seed_periodic_rule_corpus_health.py``
      with one test asserting the new entry exists in the schedules
      seed module's exported list/dict.
    scope:
      files:
        - services/api/treadmill_api/seed/schedules.py
        - services/api/tests/test_seed_periodic_rule_corpus_health.py
    validation:
      - kind: deterministic
        description: |
          The new seed entry exists verbatim and the targeted test
          passes.
        script: |
          cd services/api \
            && grep -q "periodic-rule-corpus-health" treadmill_api/seed/schedules.py \
            && uv run pytest tests/test_seed_periodic_rule_corpus_health.py -q

  - id: cancellation-filter-on-dispatch
    title: task.cancelled filter on downstream wf-* dispatches
    workflow: wf-author
    intent: |
      When a task's ``derived_status`` is in a cancelled-state, any
      subsequent ``wf-*`` workflow dispatches against that task should
      be suppressed and the suppressed dispatch recorded as an event.

      Edit ``services/api/treadmill_api/coordination/dispatch.py``. Add
      a predicate function called ``_filter_cancelled_task_dispatch``
      that takes the task_id and the workflow_id we're about to
      dispatch. The predicate:
        - looks up the task's current ``derived_status`` (via the
          ``task_status`` VIEW or however dispatch.py already accesses
          task state — pattern-match the file)
        - if derived_status indicates cancellation, return a flag
          indicating "skip this dispatch"
        - emit a new event ``cancellation_filter`` (entity_type:
          ``cancellation_filter``, action: ``recorded``) with payload
          ``{workflow_id, task_id, reason: 'task derived_status indicates cancelled'}``

      Wire the predicate into dispatch.py's main dispatch path — find
      where wf-* workflow_runs are created (search for
      ``_create_and_publish_run`` callers in this file) and call the
      predicate before each.

      Then create a NEW test file at
      ``services/api/tests/test_cancellation_filter.py`` with:
        - register a task → assert it dispatches normally to wf-author
        - put the task in a cancelled derived_status (directly UPDATE
          the source rows — task_cancelled event, or however the
          cancelled state is set today)
        - attempt another dispatch → assert no new run is created and
          the cancellation_filter event is written.
    scope:
      files:
        - services/api/treadmill_api/coordination/dispatch.py
        - services/api/tests/test_cancellation_filter.py
    validation:
      - kind: deterministic
        description: |
          The predicate exists in dispatch.py and the targeted tests
          pass.
        script: |
          cd services/api \
            && grep -q "_filter_cancelled_task_dispatch\|cancellation_filter" treadmill_api/coordination/dispatch.py \
            && uv run pytest tests/test_cancellation_filter.py -q
```

## Notes on validation script shape

Every replacement validation does only two things:

1. **One small literal grep** — confirms the worker actually wrote the load-bearing string (function name, class name, role id). No "did they touch this file" greps; no exact-string-of-prose greps.
2. **One pytest run on ONLY the new test file** — the test the worker is asked to create alongside the implementation. This file is named in the intent's scope.files block, so the validation script is deterministic about which test to run.

No validation script runs `pytest tests/test_starters.py -q` (the whole-suite anti-pattern). No validation depends on prior tests passing. Every test the validation runs is a test the worker created.

## After Joe reviews

When approved:
1. Cancel the five in-flight stuck task runs (or let them cap out — operator preference)
2. Submit this plan via ``treadmill plan submit --doc docs/plans/2026-05-19-rework-impasse-tasks.md --repo joeLepper/treadmill``
3. Watch the pipeline; the autoscaler will spin up workers and the architect-recovery loop will engage if any task hits the same impasse class
