---
status: drafting
trigger: ADR-0036 accepted same-day. Operationalizes the two coupled axes (single-channel verdict + kind-aware rules) and the standing-decision-not-implemented severity gating from ADR-0029 Q29.f. Held until ready to dispatch.
---

# Plan: Hands-free review and validation discipline (ADR-0036 execution)

- **Status:** drafting
- **Date:** 2026-05-15
- **Related ADRs:** ADR-0036 (parent), ADR-0006, ADR-0013, ADR-0027, ADR-0029, ADR-0031, ADR-0033

## Goal

Land the four implementation pieces ADR-0036 commits us to (severity-aware mergeability, per-rule applicability, single-channel review prose, PR body synthesis) and prove convergence by re-running the auto-merge smoke against both a default-enabled plan and an opt-out plan.

## Success criteria

- A trivial default-enabled bot PR cycles author → review (`approved`) → validate (`pass` over applicable+blocking rules only) → mergeability `mergeable` → 30s cool-off → auto-merge fires → PR `MERGED` with zero operator action.
- A trivial opt-out bot PR (plan frontmatter `auto_merge: false`) reaches the same gate state but the auto-merge predicate skips per ADR-0031 Q31.c; the PR sits open for operator merge.
- The `task_mergeability` VIEW returns `mergeable` when applicable+blocking rules pass even if non-applicable or warning-severity rules fail; smoke-equivalent regression test demonstrates this.
- The review disposition's gh-pr-comment body is generated from `ReviewVerdict` + diff context, not from the model's free narrative. No path exists where prose disagrees with verdict.
- Every rule under `tools/rule-checks/<id>/` carries an `applies_to:` declaration. wf-validate's aggregate output ignores rules whose applicability doesn't intersect the PR's derived kinds.

## Constraints / scope

### In scope
- Six tasks below.
- One alembic migration (VIEW rewrite).
- One worker disposition rewrite (review prose synthesis).
- One worker disposition addition (code PR-body synthesis).
- Rule-manifest schema extension + one-shot corpus migration (every existing rule gains `applies_to:`).
- Deterministic PR-kind extractor.
- Smoke handoff doc.

### Out of scope
- Reviewer model-quality improvements (better prompts, larger models). Consistency comes from the boundary layer; quality is a separate concern.
- Moving rules from the filesystem to the DB (ADR-0006 storage stays as-is).
- Per-author or per-repo rule exemptions (kind-based is the only axis we're adding).
- Surfacing per-rule applicability decisions in the PR-comment body (operator UX follow-up).
- The reviewer's prose narrative being replaced by something richer than synthesis (we may revisit if synthesis is too terse).
- The pre-existing pending observability stack, ADR-0034 crystallization, ADR-0035 scheduler — those queue behind hands-free convergence.

### Budget
One session for the operator to dispatch + observe. **NOT dispatched until hands-free converges manually first** — we want to know the cascade works before we trust it to drive itself.

## Diagram

See ADR-0036 §Diagram for the actor handoff. This plan does not duplicate it; tasks below reference the actors by name (Author, Reviewer, ReviewDisposition, Validator, RuleEngine, Mergeability, AutoMerge).

## Sequence of work

```yaml
sequence_of_work:
  - id: severity-gating-in-view
    title: Mergeability VIEW honors per-check severity (ADR-0029 Q29.f)
    workflow: wf-author
    intent: |
      Rewrite the ``task_mergeability`` VIEW so the
      ``validate_decision`` projection aggregates over
      ``severity=blocking`` checks only. ``warning`` and
      ``advisory`` failures appear in the step output but do
      not propagate to the VIEW's aggregate decision.

      Concretely: the lateral subquery for ``validate`` in
      ``alembic/versions/0006_task_mergeability_view.py`` joins
      to a per-check materialization (probably via JSONB
      extraction over ``output->'payload'->'checks'``) and
      aggregates ``MAX(verdict)`` only where severity is
      blocking. The aggregate verdict maps ``fail→fail``,
      otherwise ``pass``.

      Add an integration test demonstrating the new behavior:
      a wf-validate step.completed with one ``severity=blocking
      verdict=pass`` and one ``severity=warning verdict=fail``
      yields ``validate_decision=pass`` (was ``fail`` before).
    scope:
      files:
        - services/api/alembic/versions/0013_severity_aware_mergeability.py
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/tests/test_integration_task_mergeability.py
    validation:
      - kind: deterministic
        description: |
          Migration runs; tests pass.
        script: |
          ( cd services/api && uv run alembic upgrade head ) \
            && ( cd services/api && uv run pytest tests/test_integration_task_mergeability.py -q )

  - id: pr-kind-extractor
    title: Deterministic PR-kind derivation from diff
    workflow: wf-author
    intent: |
      New module ``workers/agent/treadmill_agent/pr_kinds.py``
      exporting ``derive_kinds(diff_paths: list[str]) -> set[str]``.
      Returns a subset of ``{code, docs-only, test-only, infra,
      migration}`` per the algorithm:

        - ``migration`` if any path matches ``alembic/versions/``
        - ``test-only`` if all changed paths are under ``tests/``
        - ``docs-only`` if all changed paths are under ``docs/``
          or end in ``.md``
        - ``infra`` if any path matches ``infra/`` or ``Dockerfile``
        - ``code`` otherwise (the default catch-all)

      A PR may have multiple kinds (e.g., ``code`` + ``migration``).
      Conservatism: ``code`` always wins as a tiebreaker for
      mixed PRs; ``docs-only`` requires every changed path to
      qualify.

      Tests in ``test_pr_kinds.py`` covering the algorithm
      with representative diff fixtures.
    scope:
      files:
        - workers/agent/treadmill_agent/pr_kinds.py
        - workers/agent/tests/test_pr_kinds.py
    validation:
      - kind: deterministic
        description: |
          Tests pass.
        script: |
          cd workers/agent && uv run pytest tests/test_pr_kinds.py -q

  - id: rule-manifest-applicability
    title: Rule manifests declare applies_to + wf-validate filters by it
    workflow: wf-author
    depends_on:
      - task.pr-kind-extractor.pr_merged
    intent: |
      Two changes:

      1. Extend every rule manifest under ``tools/rule-checks/<id>/``
         with an ``applies_to: [kind, ...]`` field. The schema
         lives in ``tools/rule-checks/_schema.json`` (extend
         it). One-shot corpus migration: each of the ~12 rules
         gets a sensible default — ``code`` for test-coverage
         rules, ``[code, docs-only, infra]`` for the
         pr-description-conforms rule, etc.

      2. wf-validate's aggregate disposition reads the PR's
         kinds (from ``pr_kinds.derive_kinds`` against the diff
         paths) and filters: only checks whose ``applies_to``
         intersects the PR's kinds run. Non-applicable rules
         appear in the step output's payload as
         ``verdict=skipped`` but do not propagate to the
         aggregate.

      Tests in ``test_rule_applicability.py``: a docs-only PR
      should not see ``tests-exercise-success-criteria`` fire.
    scope:
      files:
        - tools/rule-checks/_schema.json
        - tools/rule-checks/agent-md-locations/manifest.json
        - tools/rule-checks/agent-md-section-presence/manifest.json
        - tools/rule-checks/features-ship-with-tests/manifest.json
        - tools/rule-checks/pr-description-conforms/manifest.json
        - tools/rule-checks/surface-changes-have-doc-updates/manifest.json
        - tools/rule-checks/tests-exercise-success-criteria/manifest.json
        - tools/rule-checks/implementation-conforms/manifest.json
        - tools/rule-checks/purpose-articulated-in-collapse-proposal/manifest.json
        - workers/agent/treadmill_agent/runner_dispositions/validation.py
        - workers/agent/tests/test_rule_applicability.py
    validation:
      - kind: deterministic
        description: |
          Every rule manifest validates against the extended schema;
          applicability filter tests pass.
        script: |
          cd workers/agent && uv run pytest tests/test_rule_applicability.py -q

  - id: review-prose-synthesis
    title: Review disposition synthesizes gh-pr-comment body from ReviewVerdict
    workflow: wf-author
    intent: |
      Today ``review.py`` passes the model's whole summary
      (minus the JSON fence) into ``gh pr comment``. That's
      the channel-divergence vector ADR-0036 closes.

      New behavior: the disposition emits a structured body
      from a template. Sections:

        ## Treadmill review verdict: <approve|request changes>

        <one-paragraph rationale from envelope>

        <if request_changes: an "Issues" subsection listing the
        concrete asks; derived from rationale by simple
        sentence-split, OR an additional "issues:" field on
        ReviewVerdict if the model surfaces multiple>

      The model's summary becomes structured-output INPUT
      (rationale field of the envelope), not free-text output.

      Tests in ``test_review_disposition_prose_synthesis.py``:
      identical prose body for identical verdicts; verdict
      never disagrees with prose.
    scope:
      files:
        - workers/agent/treadmill_agent/runner_dispositions/review.py
        - workers/agent/tests/test_runner_dispositions.py
    validation:
      - kind: deterministic
        description: |
          Tests pass; review-disposition tests still green.
        script: |
          cd workers/agent && uv run pytest tests/test_runner_dispositions.py -q

  - id: code-disposition-pr-body-synthesis
    title: Code disposition synthesizes 5-section PR body from task context
    workflow: wf-author
    intent: |
      Today ``code.py`` passes
      ``ctx.claude_result.summary or ctx.ctx.title`` into
      ``gh pr create --body``. Per ADR-0033 the body must
      have ``## Summary / ## Why / ## Test plan / ## Validation
      / ## Refs``. The model knows this but doesn't honor it.

      New behavior: ``_synthesize_pr_body(ctx)`` produces the
      5-section template from:

        ## Summary       — ctx.ctx.title + first line of model summary
        ## Why           — plan_doc_path + task description
        ## Test plan     — bulleted checklist from validation scripts
        ## Validation    — fenced block of each validation script
        ## Refs          — task id, plan path, branch

      Tests in ``test_code_disposition_pr_body.py``.
    scope:
      files:
        - workers/agent/treadmill_agent/runner_dispositions/code.py
        - workers/agent/tests/test_runner_dispositions.py
    validation:
      - kind: deterministic
        description: |
          Tests pass.
        script: |
          cd workers/agent && uv run pytest tests/test_runner_dispositions.py -q

  - id: author-side-fail-dispatches-feedback
    title: wf-author.fail dispatches wf-feedback (ADR-0037)
    workflow: wf-author
    intent: |
      Per ADR-0037, when a code-emitting step (wf-author /
      wf-feedback / wf-ci-fix / wf-conflict) completes with
      ``decision='fail'`` because the task's author-side
      validation script returned non-zero, dispatch
      ``wf-feedback`` for the same task. Cap applies per
      ADR-0029 Q29.e (shared 5-attempt budget across all
      wf-feedback sources).

      Implementation: extend
      ``coordination/consumer._maybe_fire_validate_feedback``
      (rename to ``_maybe_fire_step_feedback`` or add a
      sibling) and ``coordination/triggers.maybe_dispatch_feedback_on_terminal_failure``
      to recognize the (wf-author, fail) pair. Dedup
      namespace ``wf-feedback:<repo>:author-fail-run=<run_id>``
      per ADR-0026.

      Tests: integration test that a wf-author step.completed
      with decision=fail (from author-side validation)
      dispatches wf-feedback exactly once and respects the
      shared 5-attempt cap.

      Companion learning ``docs/learnings/2026-05-14-author-side-fail-no-remediation.md``
      transitions to ``crystallized-into-ADR-0037``.
    scope:
      files:
        - services/api/treadmill_api/coordination/consumer.py
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/treadmill_api/coordination/dispatch_dedup.py
        - services/api/tests/test_integration_event_triggers.py
    validation:
      - kind: deterministic
        description: |
          Tests pass; dispatch helpers recognize the new pair.
        script: |
          ( cd services/api && uv run pytest tests/test_integration_event_triggers.py -q ) \
            && grep -q "author-fail-run\|wf-author.*fail" services/api/treadmill_api/coordination/triggers.py

  - id: hands-free-convergence-smoke
    title: End-to-end smoke proves auto-merge converges on bot trivial PRs
    workflow: wf-validate
    depends_on:
      - task.severity-gating-in-view.pr_merged
      - task.rule-manifest-applicability.pr_merged
      - task.review-prose-synthesis.pr_merged
      - task.author-side-fail-dispatches-feedback.pr_merged
      - task.code-disposition-pr-body-synthesis.pr_merged
    intent: |
      Submit two trivial plans:

        Smoke 1d (default-enabled): one task appending a
        marker line to the smoke notes file. Observe:
          - wf-author writes the change with a synthesized
            5-section PR body
          - wf-review approves; gh pr comment carries
            synthesized prose
          - wf-validate runs only applicable rules
            (``surface-changes-have-doc-updates`` should be
            skipped for docs-only); aggregate is ``pass``
          - mergeability=mergeable; 30s later, auto-merge fires
          - PR ends in state=MERGED

        Smoke 2d (opt-out, plan frontmatter
        ``auto_merge: false``): same flow except auto-merge
        skips per ADR-0031 Q31.c.

      Document both in
      ``docs/handoffs/2026-05-15-hands-free-convergence-smoke.md``
      with cycle counts, wall-clock latency, and per-rule
      applicability outcomes.
    scope:
      files:
        - docs/handoffs/2026-05-15-hands-free-convergence-smoke.md
    validation:
      - kind: deterministic
        description: |
          Handoff doc exists and cites both smokes converging.
        script: |
          test -f docs/handoffs/2026-05-15-hands-free-convergence-smoke.md \
            && grep -qi "smoke 1d" docs/handoffs/2026-05-15-hands-free-convergence-smoke.md \
            && grep -qi "smoke 2d" docs/handoffs/2026-05-15-hands-free-convergence-smoke.md \
            && grep -qi "auto.merge.fired" docs/handoffs/2026-05-15-hands-free-convergence-smoke.md \
            && grep -qi "opt.out" docs/handoffs/2026-05-15-hands-free-convergence-smoke.md
```

## Risks / unknowns

- **PR-kind misclassification.** A docs-only PR that actually changes infra slips the kind-aware filter. Mitigation: conservative algorithm (default to `code`); test the extractor against a corpus of real diffs before turning the axis on; surface kinds in the validate step's output payload so operators can spot misclassifications.
- **Synthesis prose hides reviewer reasoning.** If the disposition over-summarizes, useful context drops. Mitigation: include the model's `rationale` verbatim alongside the verdict line.
- **VIEW rewrite changes existing aggregates.** Plans currently in flight may see their mergeability flip when the migration lands. Mitigation: review which open PRs would change derived_mergeability before applying the migration; expect to converge or open trivial follow-up PRs for any that flip wrong.
- **Aggregate semantics in wf-validate worker.** The worker today emits a single `pass`/`fail` over all rule outputs; the new aggregate must skip non-applicable and ignore non-blocking. We'll abort if this turns out to require a deeper rewrite of the validation runner than expected.

## Decisions captured during execution

(Populated as we work. Each entry links to an ADR via `/decide` when a real decision emerges.)

## Post-mortem

(Filled in when the plan transitions to `completed` or `abandoned`.)
