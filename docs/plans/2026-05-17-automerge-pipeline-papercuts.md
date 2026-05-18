---
status: drafting
trigger: PRs #132 + #133 sat MERGEABLE/CLEAN for 14h on 2026-05-17 because the architect override path silently lost a transaction race. Triage of the open papercut backlog (TaskList #114–#135) surfaced 10 sibling-shaped fixes — most concentrated in the auto-merge / event-state-projection layer. Bundling so the convergence-proof regressions land together.
---

# Plan: Auto-merge pipeline papercuts

Close the gaps that let an event fire without downstream state catching up — flush races, missing event verbs, missing replays, missing dispatch predicates — plus a handful of adjacent papercuts. Front-loaded so the auto-merge pipeline reliability fixes ship first.

## Goal

After execution: (1) the architect's override authority reliably propagates to GitHub auto-merge in a single transaction; (2) cancelled tasks halt their downstream wf-* dispatches instead of spawning zombie runs; (3) CLI-dispatched plans auto-activate; (4) new projections backfill from prior `pr_merged` events; (5) closed-without-merge PRs are first-class; (6) the role-crystallization-judge tolerates prose-only output; (7) review.py's `VERDICT:` regex tourniquet is gone.

## Success criteria

- Architect `accept-as-is` on validate-fail deadlock produces auto-merge within 60s of the verdict (was: never, until manual intervention).
- A task transitioned to `cancelled` never spawns a new `wf-*` dispatch.
- `treadmill plan submit --doc` and `treadmill learnings crystallize` flip the plan's `status:` from `drafting` to `active` automatically.
- New Alembic migrations that introduce a projection replay all prior `pr_merged` events against the new VIEW on first deploy.
- `pull_request.closed` webhook with `merged=false` writes a `pr_closed` event; `task_status` VIEW reflects the closed-without-merge state.
- `role-crystallization-judge` accepts prose-only verdicts via the same fallback parser the architect uses.
- `_VERDICT_INNER_RE` is deleted from `workers/agent/treadmill_agent/runner_dispositions/review.py`; structured JSON envelope is the only parse path.

## Constraints / scope

### In scope

- 10 tasks below, in the listed order.

### Out of scope

- Bootstrap non-Treadmilled repos (TaskList #95).
- Treadmill-as-GitHub-App (TaskList #109).
- VS Code plugin (TaskList #122).
- Worker PAT workflow scope (TaskList #128) — needs runtime repro first; deferred to a follow-up if the failure mode is real.

### Budget

~10 days of focused work. Each task ~1 day. If any single task slips past 2 days, abort it and write a post-mortem rather than escalating quietly (per plan-skill convention).

## Risks / unknowns

- **Flush fix changes transaction shape** (#135) — `await session.flush()` mid-handler could expose latent issues elsewhere if other handlers depend on the implicit snapshot. Mitigation: tight integration test that asserts both override + auto-merge fire in the same transaction.
- **Replay job (#130) is O(N) on first run** — backfilling all historic `pr_merged` events against a new VIEW could be slow on large deployments. Mitigation: gate behind a manual `treadmill replay --since=<sha>` for v1, not automatic on migration.
- **task.cancelled predicate (#134) may surprise active flows** — if any in-flight runs were depending on cancelled tasks for their dispatch chain, the cap will manifest as silent dispatch drops. Mitigation: log dispatched-but-cancelled events to events table with `entity_type='cancellation_filter'`.

## Sequence of work

```yaml
sequence_of_work:
  - id: architect-override-flush-race
    title: Architect override needs session.flush() before mergeability VIEW read
    workflow: wf-author
    intent: |
      Add ``await session.flush()`` between the override-event
      INSERTs at consumer.py lines ~414–426 and the
      ``maybe_auto_merge_on_mergeable()`` call at line ~441. Promote
      the silent ``logger.debug`` bailout at triggers.py line ~1450
      to ``logger.info`` so future races are diagnosable. Add an
      integration test that asserts: architect accept-as-is on
      validate-fail deadlock → ``task_mergeability`` VIEW reflects
      override in the same transaction → auto-merge cooling-off
      deadline set within the same handler.

      Reference the 2026-05-17 learning in the commit body. This
      directly resolves the failure mode where PRs #132 + #133 sat
      MERGEABLE/CLEAN for 14 hours.
    scope:
      files:
        - services/api/treadmill_api/coordination/consumer.py
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/tests/test_integration_architect_override.py
    validation:
      - kind: deterministic
        description: |
          flush() present + bailout log promoted + integration test passes.
        script: |
          cd services/api \
            && grep -A2 "INSERT.*validate.override\|emit_validate_override" treadmill_api/coordination/consumer.py | grep -q "session.flush" \
            && grep "logger.info.*derived_mergeability" treadmill_api/coordination/triggers.py \
            && uv run pytest tests/test_integration_architect_override.py -q

  - id: task-dependencies-sql-bypass-reeval
    title: SQL-bypass writes to task_dependencies trigger re-evaluation
    workflow: wf-author
    depends_on:
      - task.architect-override-flush-race.pr_merged
    intent: |
      Sibling shape to the override flush race: direct writes to
      ``task_dependencies`` (test fixtures + any future operator
      tools) bypass the event projector and leave dispatch state
      stale. Author a SQLAlchemy after-insert / after-update hook on
      the ``TaskDependency`` model that fires a synthetic
      ``task_dependencies.changed`` event, which the redispatch
      consumer already knows how to handle.

      Tests cover: direct INSERT INTO task_dependencies → next
      consumer tick re-evaluates the affected task → dispatch fires
      if newly unblocked.
    scope:
      files:
        - services/api/treadmill_api/models/task_dependency.py
        - services/api/treadmill_api/coordination/redispatch.py
        - services/api/tests/test_integration_task_dependencies_reeval.py
    validation:
      - kind: deterministic
        description: |
          Hook wired + test passes.
        script: |
          cd services/api \
            && grep -q "task_dependencies.changed" treadmill_api/models/task_dependency.py \
            && uv run pytest tests/test_integration_task_dependencies_reeval.py -q

  - id: task-cancelled-halts-dispatches
    title: task.cancelled events filter downstream wf-* dispatches
    workflow: wf-author
    intent: |
      Add a predicate to the dispatch path
      (``services/api/treadmill_api/coordination/dispatch.py``) that
      checks the task's current ``derived_status`` before firing
      ``wf-*`` runs. If the task is in a cancelled state, skip the
      dispatch and write an ``entity_type='cancellation_filter'``
      event recording the suppressed dispatch (workflow_id, reason).

      Tests cover: register a task → cancel it → events that would
      have triggered downstream dispatches → no new runs spawn → the
      cancellation_filter event is written.
    scope:
      files:
        - services/api/treadmill_api/coordination/dispatch.py
        - services/api/tests/test_integration_cancellation_filter.py
    validation:
      - kind: deterministic
        description: |
          Predicate present + test passes.
        script: |
          cd services/api \
            && grep -q "cancellation_filter\|task.derived_status.*cancelled" treadmill_api/coordination/dispatch.py \
            && uv run pytest tests/test_integration_cancellation_filter.py -q

  - id: cli-plans-auto-activate
    title: CLI plan submissions auto-flip status drafting → active
    workflow: wf-author
    intent: |
      ``treadmill plan submit --doc`` and
      ``treadmill learnings crystallize`` currently leave the plan's
      frontmatter at ``status: drafting``; an operator (or the
      manual activation PR pattern from #107 / #108) must flip it
      before workers pick up the tasks.

      Patch both CLI commands to auto-promote ``status: drafting`` →
      ``status: active`` as part of the submission flow, by rewriting
      the frontmatter and including the bump in the PR (or, for
      ``--dev``, writing it locally). Skip if status is already
      ``active`` or downstream (idempotent).

      Tests parametrize: a doc with drafting → submission flips it;
      a doc already active → no change; a doc completed → submission
      refused with a clear error.
    scope:
      files:
        - cli/treadmill_cli/commands/plan.py
        - cli/treadmill_cli/commands/learnings.py
        - cli/tests/test_plan_submit_auto_activate.py
    validation:
      - kind: deterministic
        description: |
          Auto-activate logic + tests pass.
        script: |
          cd cli && uv run pytest tests/test_plan_submit_auto_activate.py -q

  - id: reconcile-db-task-state-with-pr-reality
    title: Reconcile task_status VIEW with GitHub-side merge reality
    workflow: wf-author
    intent: |
      Two known divergences between DB state and GitHub state:
      (a) tasks marked ``done`` in clause 6d of task_status when
      wf-author completed without producing a PR (silent failure
      misread as success — seen on tasks 209f4d8e and 0739cee3 on
      2026-05-16); (b) operator-completed PRs whose merge wasn't
      projected to ``derived_status`` (the projection lag visible on
      ~6 ``pr_opened`` tasks whose PRs have already merged).

      Pre-step verification: confirm both divergences are still
      reproducible against current main HEAD before changing the
      VIEW (the recent ``task_status view: distinguish decision=fail
      from done`` merge in #121 may have already addressed (a)).

      Fix: extend task_status clause 6d to look at the latest step's
      output payload (``validation_results[].verdict``) — silent fail
      surfaces as ``silently-failed`` rather than ``done``. Add a
      reconciler job that, on each pr_merged event, projects to
      task_status for any task whose branch matches the merged PR's
      ``head.ref`` regardless of whether ``task_prs`` row exists.
    scope:
      files:
        - services/api/alembic/versions/0019_task_status_reconciliation.py
        - services/api/treadmill_api/coordination/reconcile.py
        - services/api/tests/test_integration_task_state_reconcile.py
    validation:
      - kind: deterministic
        description: |
          Migration applies + reconciler test passes.
        script: |
          cd services/api && uv run alembic upgrade head \
            && uv run pytest tests/test_integration_task_state_reconcile.py -q

  - id: pr-closed-event-verb-and-projection
    title: pr_closed event verb + task_status projection for closed-without-merge
    workflow: wf-author
    intent: |
      The webhook normalizer at
      ``services/api/treadmill_api/webhooks/normalize.py`` lines
      ~99–116 currently emits ``pr_merged`` for ``action=closed AND
      merged=true`` but silently drops ``action=closed AND
      merged=false``. Introduce a ``pr_closed`` event verb covering
      the latter; project to ``task_status`` so the affected task
      surfaces as ``pr_closed_without_merge`` (a new terminal
      derived_status) rather than sticking at ``pr_opened`` forever.

      Tests: webhook with merged=false → pr_closed event → task
      reaches the new terminal state.
    scope:
      files:
        - services/api/treadmill_api/webhooks/normalize.py
        - services/api/alembic/versions/0020_pr_closed_event.py
        - services/api/tests/test_integration_pr_closed.py
    validation:
      - kind: deterministic
        description: |
          Event verb + projection + tests.
        script: |
          cd services/api && uv run alembic upgrade head \
            && uv run pytest tests/test_integration_pr_closed.py -q

  - id: replay-pr-merged-on-new-projection
    title: Replay pr_merged events when a new projection lands
    workflow: wf-author
    depends_on:
      - task.pr-closed-event-verb-and-projection.pr_merged
    intent: |
      New Alembic migrations that introduce VIEW projections
      currently start empty even when prior ``pr_merged`` events
      could populate them. Add a ``treadmill replay --event-type
      pr_merged --since <sha>`` CLI subcommand that re-projects
      historic events against the current VIEW.

      Per Q34.c in ADR-0034, replay is operator-dispatched, not
      automatic on migration. The migration's own ``upgrade()`` may
      log a hint pointing operators to the replay command.

      Tests: seed N pr_merged events → run replay → assert VIEW
      now reflects all N projections.
    scope:
      files:
        - cli/treadmill_cli/commands/replay.py
        - services/api/treadmill_api/coordination/replay.py
        - cli/tests/test_replay_command.py
    validation:
      - kind: deterministic
        description: |
          Replay command + test passes.
        script: |
          cd cli && uv run pytest tests/test_replay_command.py -q

  - id: crystallization-judge-prose-fallback
    title: role-crystallization-judge gains prose-verdict fallback parser
    workflow: wf-author
    intent: |
      ``workers/agent/treadmill_agent/runner_dispositions/crystallization.py``
      currently raises ``CrystallizationVerdictParseError`` if the
      judge's output lacks a fenced JSON envelope (lines ~67–93).
      The role-architect handles the same problem with a
      ``_PROSE_VERDICT_CUES`` list at ``architecture.py`` lines
      ~79–100, defaulting to ``uncertain`` when no JSON is found but
      a prose verdict is present.

      Backport the architect's fallback parser to crystallization,
      mapping prose to the three crystallization verdicts (``ready``
      / ``not-ready`` / ``defer``). Tests parametrize: prose-only
      output for each verdict.
    scope:
      files:
        - workers/agent/treadmill_agent/runner_dispositions/crystallization.py
        - workers/agent/tests/test_runner_dispositions.py
    validation:
      - kind: deterministic
        description: |
          Fallback parser + tests.
        script: |
          cd workers/agent && uv run pytest tests/test_runner_dispositions.py -q -k crystallization

  - id: delete-verdict-regex-tourniquet
    title: Delete _VERDICT_INNER_RE regex from review.py
    workflow: wf-author
    intent: |
      Per ADR-0028 (structured step output), the
      ``_VERDICT_INNER_RE = re.compile(r"^VERDICT:\s*..."`` regex at
      ``workers/agent/treadmill_agent/runner_dispositions/review.py``
      was supposed to be deleted in favor of fenced JSON. Multiple
      callsites still fall back through it; the migration is
      incomplete.

      Delete the regex and every callsite that still references it.
      Replace any test that asserted prose parsing with the
      structured-envelope assertion.
    scope:
      files:
        - workers/agent/treadmill_agent/runner_dispositions/review.py
        - workers/agent/tests/test_runner_dispositions.py
    validation:
      - kind: deterministic
        description: |
          Regex gone + tests pass.
        script: |
          cd workers/agent \
            && ! grep -q "_VERDICT_INNER_RE" treadmill_agent/runner_dispositions/review.py \
            && uv run pytest tests/test_runner_dispositions.py -q -k review

  - id: depends-on-realistic-chain-smoke
    title: Smoke — depends_on enforcement under a realistic 3-task chain
    workflow: wf-validate
    intent: |
      The existing integration test in
      ``services/api/tests/test_integration_plans_router.py:605``
      covers a single ``t1 → t0`` dependency. Extend coverage with a
      3-task chain (``t2 → t1 → t0``): assert that t2 is blocked
      until t1's PR merges; t1 is blocked until t0's PR merges; and
      no t2 dispatch fires before t0 + t1 are both ``pr_merged``.

      Document the cycle in
      ``docs/handoffs/2026-05-17-depends-on-chain-smoke.md``.
    scope:
      files:
        - services/api/tests/test_integration_depends_on_chain.py
        - docs/handoffs/2026-05-17-depends-on-chain-smoke.md
    validation:
      - kind: deterministic
        description: |
          Chain smoke test passes.
        script: |
          cd services/api && uv run pytest tests/test_integration_depends_on_chain.py -q
```

## Decisions captured during execution

(empty)

## Post-mortem

Filled in on transition to `completed`/`abandoned`.
