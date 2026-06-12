---
auto_merge: false
---

# Plan: ADR-0090 — evaluator wake-filter + coordinator CI-observer decouple

- **Status:** drafting
- **Date:** 2026-06-12
- **Related ADRs:** ADR-0090 (this implements it), ADR-0089 (wake-filter
  mechanism), ADR-0087 (team model), ADR-0063 (deferred `(repo, head_sha)`
  FK — task `head-sha-resolver` is its first consumer)

## Goal

Cut the dominant remaining opus burn (coordinator + evaluator waking on
~13 `check_run.completed` per PR) so software teams can run in parallel
again under the Claude 5h limit — without blinding CI tracking.

## Success criteria

- A PR that triggers N check-runs produces **one** `task.ci_result` wake for
  the coordinator, not N.
- The coordinator still advances the step on suite-success and opens rework
  on suite-failure (behavior preserved, now keyed on the rollup).
- The evaluator no longer wakes on `check_run.completed` / `pr_synchronize`;
  it still wakes on `ci_result`, verdicts, review handoffs, escalations.
- `wake ⊇ relay` invariant stays green for both roles; no escalation-class
  action is ever filtered.

## Constraints / scope

### In scope
ADR-0090 §1 (evaluator filter) + §2 (CI-observer decouple) only.

### Out of scope
ADR-0091 team scheduler (separate fast-follow plan); worker filtering; model
routing. The manual team pause stands until 0091 automates it.

### Budget
~4 worker-days. Abort to a post-mortem if `head-sha-resolver` reveals the
lookup needs a migration larger than one nullable column.

## Sequence of work

```yaml
sequence_of_work:
  - id: head-sha-resolver
    title: "(repo, head_sha) -> task_pr -> task resolver"
    workflow: wf-implement
    depends_on: []
    intent: |
      Confirm whether ``task_prs`` already carries ``head_sha`` (read
      ``services/api/treadmill_api/models/task.py``). If it does, add a
      resolver ``resolve_task_by_head_sha(session, repo, sha)`` and note
      the existing column in the PR description. If it does NOT, add a
      nullable ``head_sha VARCHAR`` column via an Alembic migration
      mirroring ``services/api/alembic/versions/20260611_0300_*`` (safe on
      an empty/partial table), then add the resolver. This is the
      ADR-0063-deferred ``(repo, head_sha)`` lookup; ADR-0090 is its first
      consumer. Resolver returns the task for the most-recent task_pr
      matching (repo, sha), else None.
      Add tests to the existing API test module: present-sha -> task;
      unknown-sha -> None; two task_prs same sha -> most-recent.
      Update ``services/api/AGENT.md`` (Key surfaces + Recent changes).
    scope:
      files:
        - services/api/treadmill_api/models/task.py
        - services/api/AGENT.md
    validation:
      - kind: deterministic
        description: resolver unit tests pass (present / unknown / same-sha)
        script: cd services/api && python -m pytest tests/ -k "head_sha or resolve_task" -q

  - id: ci-observer
    title: "Non-LLM CI-observer emits task.ci_result on suite completion"
    workflow: wf-implement
    depends_on:
      - task.head-sha-resolver.pr_merged
    intent: |
      Add an always-on (control-plane, NOT a team unit per ADR-0091 §2)
      observer that consumes ``github.check_run.completed``, computes
      check-SUITE completion (multiple suites, reruns, and the
      netlify-vs-CI distinction seen 2026-06-12), and POSTs a
      ``task.ci_result`` event carrying the suite's overall
      success/failure, attributed via the head-sha resolver. Idempotent:
      at most one ``ci_result`` per (task, suite-completion).
      Tests MUST run against CAPTURED check-suite payloads (not synthetic):
      exactly one ci_result per completed suite; correct task attribution;
      correct rollup incl. a mixed (one failed check) suite -> failure.
      Update ``tools/local-adapter/AGENT.md``.
    scope:
      files:
        - tools/local-adapter/AGENT.md
    validation:
      - kind: deterministic
        description: observer unit tests pass (one ci_result/suite, attribution, rollup)
        script: cd tools/local-adapter && python -m pytest tests/ -k "observer or ci_result" -q

  - id: coordinator-ci-result-handler
    title: "Coordinator: per-check handler -> task.ci_result rollup handler"
    workflow: wf-implement
    depends_on:
      - task.ci-observer.pr_merged
    intent: |
      The coordinator's section 3.5 ``check_run.completed`` handler today
      does per-check side-effects (verified 2026-06-12: advance the step on
      the last successful required check; open coordinator-rework on a
      non-success conclusion). Replace it with a ``task.ci_result`` handler
      that fires those same decisions ONCE on the suite rollup: advance on
      suite-success, open rework on suite-failure. Remove the per-check
      advance/rework logic. Do NOT change wake config here (next task) --
      only the handler semantics, so the coordinator consumes ci_result
      before it stops waking on check_run. Update the template AGENT.md.
    scope:
      files:
        - tools/team-templates/coordinator/CLAUDE.md.tmpl
        - tools/team-templates/coordinator/AGENT.md
    validation:
      - kind: deterministic
        description: template keys off the ci_result rollup (coarse presence)
        script: grep -q "task.ci_result" tools/team-templates/coordinator/CLAUDE.md.tmpl

  - id: wake-allowlists
    title: "Evaluator + coordinator wake allowlists (drop check_run noise)"
    workflow: wf-implement
    depends_on:
      - task.coordinator-ci-result-handler.pr_merged
    intent: |
      In ``tools/cc-channel-treadmill/wake-filter.ts`` add role defaults for
      ``coordinator`` and ``evaluator`` mirroring the ADR-0089 orchestrator
      pattern. Coordinator: include ``task.ci_result`` + every
      decision/lifecycle/escalation class it acts on (pr_merged, pr_opened,
      task.*_verdict, task.escalat*, the enumerated escalation actions,
      plan/task lifecycle); EXCLUDE ``github.check_run_completed`` and
      ``github.pr_synchronize``. Evaluator: same decision/escalation set +
      ``task.ci_result`` + review-handoff actions; EXCLUDE the same two
      classes. Drop those two classes from BOTH roles' relay sets too so
      ``wake >= relay`` holds (ADR-0089). Wire the role default through
      ``launch-session.sh`` as the orchestrator default already is.
      Extend ``wake-filter.test.ts``: assert each new role's wake set, the
      wake-superset-of-relay invariant, and that EVERY escalation-class
      action still wakes (forbidden-failure-mode guard).
    scope:
      files:
        - tools/cc-channel-treadmill/wake-filter.ts
        - tools/cc-channel-treadmill/wake-filter.test.ts
        - tools/cc-channels/launch-session.sh
    validation:
      - kind: deterministic
        description: wake-filter suite green incl. new role defaults + invariants
        script: cd tools/cc-channel-treadmill && bun test
```

## Risks / unknowns

- **head-sha-resolver** is load-bearing: if `head_sha` isn't on `task_prs`,
  `ci-observer` can't attribute `ci_result`. Sequenced first.
- **Suite-completion logic** must mirror the coordinator's current behavior
  across multi-suite / rerun / partial-required cases — pinned with captured
  payloads, not synthetic.
- **Ordering**: `wake-allowlists` lands only after
  `coordinator-ci-result-handler` (enforced by `depends_on`); a premature
  filter blinds CI tracking.

## Diagram

Intent layer captured in ADR-0090; this plan is the task sequencing.
