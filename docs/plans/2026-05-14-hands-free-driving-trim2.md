---
status: completed
trigger: |
  Mega-plan re-fire iteration 2. First trim was blocked by missing
  uv (fixed in 75ce71e). Second iteration tasks went "done" with
  no PRs — haiku produced plausible-prose-but-no-deliverable.
  Operator-bumped role-code-author to sonnet 4.6 (DB UPDATE +
  starters.py edit). Validation scripts strengthened to catch
  the slop class — substance checks (grep for function names,
  required strings, wired entrypoints) plus pytest, so the
  agent can't satisfy validation by writing prose-without-files.
parent: docs/adrs/0031-auto-merge-on-mergeable.md
related: docs/adrs/0032-documentarian-and-architect-roles.md, docs/adrs/0033-git-artifact-discipline.md
supersedes:
  - docs/plans/2026-05-14-hands-free-driving.md
---

# Plan: Hands-free driving — trim 2 (9 remaining tasks)

Re-fire of the truly-unshipped tasks from the parent mega-plan after the worker-container `uv` gap blocked author-side validation. Same 4-phase shape; phase-1 (ADR-0033) + most of phases 2 and 3 already shipped, so this plan only carries phase-2-remainder + phase-4.

## Goal

Same as parent mega-plan: hands-free auto-merge with wf-doc-amend + wf-architecture-resolve dispositions live + validator-remediation dispatch + the recursive backfill.

## Success criteria

- `workers/agent/treadmill_agent/runner_dispositions/documentation.py` exists; handles wf-doc-amend.
- `workers/agent/treadmill_agent/runner_dispositions/architecture.py` exists; handles wf-architecture-resolve with verdict routing.
- `coordination/triggers.py` dispatches wf-doc-amend on docs-current-with-pr rule failure (4th dispatch source).
- ADR-0030 backfill plan re-routed through wf-doc-amend on all 33 tasks.
- Phase 4 auto-merge trigger + opt-out parser + dedup event + smoke all land.

## Constraints / scope

### In scope

The 9 unshipped tasks below.

### Out of scope

- Already-shipped phase-1/2/3 tasks (output-kind, ArchitectVerdict, roles/workflows, prereqs #120/#121/#124/#127).
- ADR-0024 (auto-redeploy watcher) — has its own active plan.

### Budget

One operator session. Worker image now has uv (commit 75ce71e); author-side validation should no longer block on environment.

## Diagram

See ADR-0031 §Diagram + ADR-0032 §Diagram.

## Risks / unknowns

- **Other environmental gaps in the worker container** beyond uv. Mitigation: validation scripts use widely-available tools (uv, pytest, grep, test). If we hit another gap, patch Dockerfile + redeploy.
- **wf-architecture-resolve disposition depends on roles-and-workflows-in-starters** which IS in main. Phase 2's roles/workflows config should be reachable from the cloned repo at task time.

## Sequence of work

```yaml
sequence_of_work:
  - id: wf-doc-amend-disposition
    title: workers documentation.py handles wf-doc-amend + Class C escalation
    workflow: wf-author
    intent: |
      Author
      ``workers/agent/treadmill_agent/runner_dispositions/documentation.py``.

      Disposition responsibilities (per ADR-0032 §wf-doc-amend):
        1. Read step output (amended doc artifact).
        2. git add + push + open/update PR (respecting #120's
           idempotency — which is live in main now).
        3. If agent flags Class C in step output metadata:
           write ``docs/learnings/<date>-<slug>-gap.md``;
           dispatch wf-architecture-resolve against same task.

      Wire from runner.py on output_kind == 'documentation' (the
      enum value shipped in PR #51).

      Tests parametrized over Class A/B (no escalation) and
      Class C (learning + dispatch).
    scope:
      files:
        - workers/agent/treadmill_agent/runner_dispositions/documentation.py
        - workers/agent/treadmill_agent/runner.py
        - workers/agent/tests/test_runner_dispositions.py
    validation:
      - kind: deterministic
        description: |
          Substance check: documentation.py exists with a ``handle``
          entrypoint, references the Class C escalation target
          ``wf-architecture-resolve``, is wired in runner.py on
          output_kind ``documentation``, and the dispositions test
          suite exercises it (at least one test name contains
          ``documentation``). The strict greps are deliberate —
          earlier attempts produced plausible prose without the
          deliverable.
        script: |
          cd workers/agent \
            && test -f treadmill_agent/runner_dispositions/documentation.py \
            && grep -q "def handle" treadmill_agent/runner_dispositions/documentation.py \
            && grep -qE "wf-architecture-resolve|architecture_resolve" treadmill_agent/runner_dispositions/documentation.py \
            && grep -qE "Class C|class_c|escalat" treadmill_agent/runner_dispositions/documentation.py \
            && grep -qE "documentation|DOCUMENTATION" treadmill_agent/runner.py \
            && uv run pytest tests/test_runner_dispositions.py -q -k documentation \
            && uv run pytest tests/test_runner_dispositions.py -q

  - id: wf-architecture-resolve-disposition
    title: workers architecture.py + verdict routing
    workflow: wf-author
    intent: |
      Author
      ``workers/agent/treadmill_agent/runner_dispositions/architecture.py``.

      Parse architect step output as ``ArchitectVerdict`` envelope
      (shipped in PR #52). Route per verdict:

        - amend → dispatch wf-plan to author remediation plan
        - supersede → dispatch wf-doc-amend to author superseding
          ADR + update original's status header
        - accept-as-is → dispatch wf-doc-amend to append to
          AGENT.md Pitfalls + leave PR comment via pr_comment
          helper (shipped in PR #49) tagged
          [treadmill:wf-architecture-resolve:accept-as-is]
        - uncertain → re-dispatch wf-architecture-resolve
          (cap 5); on 5th, leave PR comment
          [treadmill:wf-architecture-resolve:capped] + stop.

      Tests parametrized over the 4 verdicts.
    scope:
      files:
        - workers/agent/treadmill_agent/runner_dispositions/architecture.py
        - workers/agent/treadmill_agent/runner.py
        - workers/agent/tests/test_runner_dispositions.py
    validation:
      - kind: deterministic
        description: |
          Substance check: architecture.py exists with a ``handle``
          entrypoint, parses ``ArchitectVerdict``, routes all four
          verdicts (amend / supersede / accept-as-is / uncertain),
          enforces a per-task rework cap, is wired in runner.py,
          and the test suite parametrizes over the four verdicts.
        script: |
          cd workers/agent \
            && test -f treadmill_agent/runner_dispositions/architecture.py \
            && grep -q "def handle" treadmill_agent/runner_dispositions/architecture.py \
            && grep -q "ArchitectVerdict" treadmill_agent/runner_dispositions/architecture.py \
            && grep -q '"amend"' treadmill_agent/runner_dispositions/architecture.py \
            && grep -q '"supersede"' treadmill_agent/runner_dispositions/architecture.py \
            && grep -q '"accept-as-is"' treadmill_agent/runner_dispositions/architecture.py \
            && grep -q '"uncertain"' treadmill_agent/runner_dispositions/architecture.py \
            && grep -qE "cap|MAX_ATTEMPTS|max_attempts" treadmill_agent/runner_dispositions/architecture.py \
            && uv run pytest tests/test_runner_dispositions.py -q -k architecture \
            && uv run pytest tests/test_runner_dispositions.py -q

  - id: validator-remediation-dispatch
    title: docs-current-with-pr.fail → wf-doc-amend (fourth dispatch source)
    workflow: wf-author
    depends_on:
      - task.wf-doc-amend-disposition.pr_merged
    intent: |
      Extend ``coordination/triggers.py`` with a fourth dispatch
      source mirroring ADR-0029's third-source pattern:

        - wf-validate.step.completed with decision=fail AND
          failing check is docs-current-with-pr → dispatch
          wf-doc-amend (not wf-feedback).
        - Different rule failures still dispatch wf-feedback
          (existing path stays).
        - Extend dispatch_dedup for
          ``docs-amend-run=<run_id>``.
        - Cap remediation per task at 5.

      Tests in test_consumer_unit.py + test_dispatch_dedup.py.
    scope:
      files:
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/treadmill_api/coordination/dispatch_dedup.py
        - services/api/treadmill_api/coordination/consumer.py
        - services/api/tests/test_consumer_unit.py
        - services/api/tests/test_dispatch_dedup.py
    validation:
      - kind: deterministic
        description: |
          Substance check: triggers.py exposes a function whose
          name contains ``doc_amend``; dispatch_dedup recognizes
          the ``docs-amend-run=`` namespace; consumer wires the
          new dispatch source on docs-current-with-pr failures;
          the cap is enforced.
        script: |
          cd services/api \
            && grep -qE "maybe_dispatch_doc_amend|doc_amend_on" treadmill_api/coordination/triggers.py \
            && grep -q "docs-amend-run=" treadmill_api/coordination/dispatch_dedup.py \
            && grep -q "docs-current-with-pr" treadmill_api/coordination/consumer.py \
            && grep -qE "5|MAX_DOC_AMEND_ATTEMPTS|max_doc_amend_attempts" treadmill_api/coordination/triggers.py \
            && uv run pytest tests/test_consumer_unit.py tests/test_dispatch_dedup.py -q

  - id: rebackfill-via-doc-amend
    title: Re-fire ADR-0030 backfill plan through wf-doc-amend
    workflow: wf-author
    depends_on:
      - task.wf-doc-amend-disposition.pr_merged
      - task.wf-architecture-resolve-disposition.pr_merged
    intent: |
      Re-author
      ``docs/plans/2026-05-14-adr-0030-diagram-backfill.md`` so
      every task in sequence_of_work uses
      ``workflow: wf-doc-amend`` (replacing ``wf-author``). Bump
      frontmatter with a ``trigger:`` note. Document-only change.
    scope:
      files:
        - docs/plans/2026-05-14-adr-0030-diagram-backfill.md
    validation:
      - kind: deterministic
        description: |
          All 33 tasks now use wf-doc-amend.
        script: |
          uv run --project services/api python -c "
          import sys
          sys.path.insert(0, 'services/api')
          from treadmill_api.parsers.plan_doc import parse_plan_doc
          tasks = parse_plan_doc(open('docs/plans/2026-05-14-adr-0030-diagram-backfill.md').read())
          assert len(tasks) == 33, f'expected 33, got {len(tasks)}'
          for t in tasks:
              assert t.workflow == 'wf-doc-amend', f'{t.id} still uses {t.workflow}'
          "

  - id: prereq-snapshot
    title: Phase 4 gate — verify all phase 3 + ADR-0032 prereqs landed
    workflow: wf-author
    depends_on:
      - task.rebackfill-via-doc-amend.pr_merged
    intent: |
      Author ``docs/handoffs/2026-05-14-adr-0031-prereq-snapshot.md``
      with one section per prereq citing the merging PR + commit
      SHA: #120 (PR #53), #121 (PR #54), #124 (PR #55), #127
      (PR #56 + operator note), and ADR-0032 plan completion
      (this re-fire's wf-doc-amend + wf-architecture-resolve +
      validator-remediation + rebackfill).
    scope:
      files:
        - docs/handoffs/2026-05-14-adr-0031-prereq-snapshot.md
    validation:
      - kind: deterministic
        description: |
          Snapshot exists; names all five prereqs.
        script: |
          test -f docs/handoffs/2026-05-14-adr-0031-prereq-snapshot.md \
            && for token in "#120" "#121" "#124" "#127" "ADR-0032"; do
                 grep -q "$token" docs/handoffs/2026-05-14-adr-0031-prereq-snapshot.md \
                   || { echo "missing $token"; exit 1; }
               done

  - id: per-plan-opt-out-parser
    title: Plan-doc parser supports auto_merge frontmatter flag
    workflow: wf-author
    depends_on:
      - task.prereq-snapshot.pr_merged
    intent: |
      Extend ``parsers/plan_doc.py`` to parse optional
      ``auto_merge: bool`` from plan frontmatter (default true).
      Plumb through to ``Plan`` SQLAlchemy model.

      NOTE: The ``Plan.auto_merge`` column + Alembic migration
      0012 already landed in PR #64 (auto-merge-trigger) — that
      task needed to read the column. Confirm the column exists;
      do not create a duplicate migration.

      Document the flag in
      ``.claude/skills/plan/SKILL.md``.

      Tests in test_plan_doc_parser.py.
    scope:
      files:
        - services/api/treadmill_api/parsers/plan_doc.py
        - services/api/treadmill_api/models/plan.py
        - services/api/alembic/versions/0012_plan_auto_merge.py
        - services/api/tests/test_plan_doc_parser.py
        - .claude/skills/plan/SKILL.md
    validation:
      - kind: deterministic
        description: |
          Parser + model + migration; tests pass; skill docs flag.
        script: |
          ( cd services/api && uv run pytest tests/test_plan_doc_parser.py -q ) \
            && grep -q "auto_merge" services/api/treadmill_api/models/plan.py \
            && grep -q "auto_merge" .claude/skills/plan/SKILL.md

  - id: auto-merge-trigger
    title: maybe_auto_merge_on_mergeable in coordination/triggers.py
    workflow: wf-author
    depends_on:
      - task.prereq-snapshot.pr_merged
    intent: |
      Author ``maybe_auto_merge_on_mergeable`` in
      ``coordination/triggers.py``.

      Source: ``mergeability.changed.mergeable`` event from the
      VIEW projection (ADR-0013). Wire from consumer's projection
      handler.

      Cooling-off: 30s. Store deadline on Redis key
      ``treadmill:auto-merge-deadline:<task_id>``. On any
      wf-validate or wf-review step.completed for the task, push
      deadline forward by 30s. Consumer poll loop (5s tick)
      detects elapsed deadline → fires
      ``gh api repos/.../pulls/<n>/merge`` with method=squash.

      Skip conditions:
        - plan.auto_merge=false
        - wf-validate.decision != pass
        - pending human review
        - existing auto-merge run already dispatched for this task

      Tests in test_auto_merge_trigger.py.
    scope:
      files:
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/treadmill_api/coordination/consumer.py
        - services/api/tests/test_auto_merge_trigger.py
    validation:
      - kind: deterministic
        description: |
          Trigger function + wiring; tests pass.
        script: |
          cd services/api && uv run pytest tests/test_auto_merge_trigger.py -q \
            && grep -q "maybe_auto_merge_on_mergeable" treadmill_api/coordination/triggers.py

  - id: dispatch-dedup-and-auto-merged-event
    title: Dedup namespace + task.<id>.auto_merged event
    workflow: wf-author
    depends_on:
      - task.auto-merge-trigger.pr_merged
    intent: |
      Two additions:
        1. dispatch_dedup recognizes ``auto-merge=<task_id>``
           namespace.
        2. New event type ``task.<id>.auto_merged``:
           entity_type=task, action=auto_merged, payload
           ``{merged_sha, pr_number, repo}``. Registered in
           events/registry.py.

      Tests in test_dispatch_dedup.py +
      test_consumer_integration.py.
    scope:
      files:
        - services/api/treadmill_api/coordination/dispatch_dedup.py
        - services/api/treadmill_api/events/task.py
        - services/api/treadmill_api/events/registry.py
        - services/api/tests/test_dispatch_dedup.py
        - services/api/tests/test_consumer_integration.py
    validation:
      - kind: deterministic
        description: |
          Dedup namespace + event registered; tests pass.
        script: |
          cd services/api && uv run pytest tests/test_dispatch_dedup.py tests/test_consumer_integration.py -q

  - id: smoke-validation
    title: End-to-end smoke — auto-merge a trivial PR + verify opt-out
    workflow: wf-validate
    depends_on:
      - task.per-plan-opt-out-parser.pr_merged
      - task.dispatch-dedup-and-auto-merged-event.pr_merged
    intent: |
      Two smokes documented in
      ``docs/handoffs/2026-05-14-adr-0031-first-auto-merge.md``:

      Smoke 1 — auto-merge fires:
        Open a trivial PR (typo fix). Watch: wf-review approves,
        wf-validate passes, mergeability=mergeable, 30s elapses,
        auto-merge fires, PR state=MERGED.

      Smoke 2 — opt-out honored:
        Open a PR against a plan with ``auto_merge: false`` in
        frontmatter. Verify NO auto-merge fires.

      Record cycle counts + wall-clock latency.
    scope:
      files:
        - docs/handoffs/2026-05-14-adr-0031-first-auto-merge.md
    validation:
      - kind: deterministic
        description: |
          Handoff doc names both smoke outcomes.
        script: |
          test -f docs/handoffs/2026-05-14-adr-0031-first-auto-merge.md \
            && grep -qi "merged" docs/handoffs/2026-05-14-adr-0031-first-auto-merge.md \
            && grep -qi "opt.out\|auto_merge.*false" docs/handoffs/2026-05-14-adr-0031-first-auto-merge.md
```

## Decisions captured during execution

- 2026-05-14 — reverted `role-code-author` from sonnet back to haiku after the bump turned out to address symptoms not causes (commit `14ce8d4`).
- 2026-05-15 — wrote ADR-0036 (hands-free review and validation discipline) after the first end-to-end smoke surfaced gate misfires that the parent plan's smoke task didn't anticipate.

## Post-mortem

- **What worked.** Phases 1–3 plus four of five Phase 4 tasks landed cleanly: prereq snapshot, auto-merge cooling-off trigger (PR #64), dedup namespace + `task.<id>.auto_merged` event (PR #65), and the per-plan opt-out parser (PRs #66 + #67 + #68). Author-side validation per task #121 caught broken parser tests before they reached the PR.
- **What surprised us.** Three architectural gaps that ADR-0031's design assumed away: (a) `task_validations.script` is snapshotted at task-creation time, so plan-doc patches don't reach mid-flight tasks (operator workaround: direct DB UPDATE); (b) `validation_runtime.log_excerpt` captured stderr only, hiding pytest stdout failures across three parser re-fires (fixed in `33d9f4f`); (c) `wf-author.decision='fail'` from author-side validation has no remediation dispatch — silent stall (captured at `docs/learnings/2026-05-14-author-side-fail-no-remediation.md`).
- **What this plan teaches us about future plans.** The Phase 4 smoke task assumed the gates would converge once the auto-merge predicate landed. The first real smoke exposed gate misfires — channel divergence between reviewer prose and verdict, plus rule-set misfit on trivial bot PRs. That work moved into ADR-0036 + `docs/plans/2026-05-15-hands-free-convergence.md`, which subsumes the smoke task here. The trim2 phase-4 task `smoke-validation` is intentionally **superseded**, not abandoned — the new convergence plan re-runs it with broader scope (severity + applicability + synthesis).
- **Follow-ups.** ADR-0036 + the convergence plan are the immediate continuation. Held plans (observability, ADR-0034 crystallization, ADR-0035 scheduler) queue behind convergence.
