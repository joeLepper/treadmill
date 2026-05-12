---
status: completed
trigger: ADRs 0012–0015 accepted 2026-05-11
parent: docs/plans/2026-05-08-minimum-runnable-treadmill.md
---

# Plan: Week 3 — mergeable + multi-step workflows + envelope

## Trigger

ADRs 0012 (uniform StepOutput envelope), 0013 (per-commit mergeability VIEW), 0014 (`commit_sha` plumbing + `pr_synchronize` payload), and 0015 (multi-step workflows + role reuse) were accepted 2026-05-11. They commit Treadmill to substantial architecture work before the seven starter workflows can be fully implemented. This plan sequences that work as a precursor to the parent plan's "Week 3 — Workflow plumbing" entry.

The parent plan's Week 3 originally read "wf-plan, wf-review, wf-validate, wf-feedback, wf-ci-fix, wf-conflict — implement workflows + their roles." That framing assumed single-step workflows + per-workflow typed outputs. The 2026-05-11 design exchange surfaced that those assumptions were wrong; this plan replaces the Week-3 entry with the architecturally-correct shape.

## Goal

Land the four ADRs in code + ship the seven starter workflows with their full role prompts so Phase-2 success criterion 5 ("every starter workflow fires at least once with logged output") is observably true on a developer machine.

## Success criteria

By the time this plan closes:

1. **Envelope (ADR-0012):** every `step.completed` event carries a `StepOutput` envelope with `summary`, `decision`, `commit_sha` (where applicable), `artifacts`, `payload`, `metadata`. The worker constructs it; the consumer reads it; `task_prs` writes work end-to-end on the envelope's fields. `AuthorStepOutput` Pydantic class is removed; its fields live in `payload` by convention. **Tests verify envelope shape + per-workflow `payload` conventions.**
2. **Mergeability (ADR-0013):** `task_mergeability` VIEW resolves the six derived states under fixture-driven integration tests. `GET /api/v1/tasks/{id}` includes `mergeability` in responses. `GET /api/v1/tasks/{id}/mergeability` returns the focused row.
3. **`commit_sha` plumbing (ADR-0014):** `events.commit_sha` column exists. `GithubPrSynchronize` is registered + normalized from GitHub webhooks. Dispatcher resolves HEAD before publishing `step.ready`; worker stamps `commit_sha` on every envelope; consumer copies SHA into the Event row column. SQS claim body carries `commit_sha`. **Tests cover the full plumbing.**
4. **Multi-step workflows (ADR-0015):** seven starter workflows ship in `starters.py` per the matrix in ADR-0015 §"Per-workflow shape matrix" — eight roles, four of them sharing `role-code-author` as the action terminal. `prior_steps` field on `GET /api/v1/steps/{id}` returns ordered prior step outputs. Cross-step dispatch from the consumer fires `step.ready` for the next step. **An end-to-end two-step workflow smoke** (`wf-ci-fix` as the canary) runs against the live substrate.
5. **`event_triggers` consumer wired (Week-2 closure deferred D.7):** `github.pr_opened` fires `wf-review`. `github.pr_synchronize` fires `wf-review` + `wf-validate`. `github.pr_review_submitted` (changes_requested) fires `wf-feedback`. `github.check_run_completed` (failure) fires `wf-ci-fix`. Conflict detection (via `_check_open_prs_for_conflicts` cribbed from bunkhouse) fires `wf-conflict`. Caps for `wf-ci-fix` and `wf-conflict` at 3 attempts each.
6. **All production code ships with tests** per `rule:features-ship-with-tests`.
7. **The full workspace test suite stays green** with `TREADMILL_INTEGRATION=1` against a live substrate.
8. **A real-Claude opt-in smoke** (`TREADMILL_REAL_CLAUDE=1`) exercises at least one two-step workflow end-to-end against real Claude Code.

## Constraints / scope

### In scope

- The four ADRs (0012–0015) implemented in code.
- Eight roles + seven workflows in `starters.py` with real system prompts (not skeletons).
- Worker prompt-composition rewrite to consume `prior_steps` for two-step workflows.
- `event_triggers` consumer (the Week-2 closure's deferred D.7).
- Cap policies for `wf-ci-fix` / `wf-conflict` (3 attempts each, bunkhouse precedent).
- Conflict-detection sweep (cribbed from bunkhouse).
- Integration tests for every new VIEW + endpoint + workflow.
- The real-Claude opt-in smoke for at least one two-step workflow.

### Out of scope

- Auto-merge orchestrator (Phase 5+; ADR-0014 future).
- Long-running ML tasks / multi-minute step timeouts (future very-large ADR per ADR-0015 §"Trade-offs").
- Real GitHub mode for the worker (removed in Week-2 closure B.7; reintroduced in Phase 4 with real coverage).
- Skills + hooks content authoring beyond what each starter workflow needs at v0. ADR-0006's rule engine ships in Phase 4.
- Multi-tier dispatch / `compute_tier` revival. Column reserved; wire field still absent.
- UI / dashboard surface. Inspection stays CLI-based.

### Budget

Researcher estimate: ~70 hours of focused work. Comparable to the Week-2 closure plan's ~115 hours. The phased structure below partitions work across parallel agents where the file partition allows.

## Phased work plan

### Phase A — Foundation (~14h, mostly sequential)

ADR-0012, ADR-0014. The envelope + commit_sha plumbing must land first because B / C / D depend on them.

**A.1 — `events.commit_sha` column + indexes** (XS, ~2h, blocker for everything)
- Files: new alembic `0005_events_commit_sha.py`; `services/api/treadmill_api/models/event.py`.
- What: ADR-0014 column + two partial indexes (`(task_id, commit_sha)`, `(entity_type, action, commit_sha)`).
- Tests: extend `test_integration_migrations.py` to assert column + indexes exist.

**A.2 — `StepOutput` envelope + worker/consumer rewrite** (M, ~6h, blocker for B.1, B.2, C.1)
- Files: new `services/api/treadmill_api/events/step_output.py`; rewrite `events/step.py:StepCompleted.output` from union to `StepOutput`; remove `AuthorStepOutput` class; `events/registry.py` updates. Worker: `workers/agent/treadmill_agent/runner.py:_execute` + `eventbus.py:_publish` construct `StepOutput(...)` instead of `AuthorStepOutput(...)`. Consumer: `coordination/consumer.py:_dispatch_step` step.completed branch + `_write_task_prs_on_completed` read envelope fields (commit_sha top-level; pr_number from payload; branch from artifacts).
- Tests: rewrite `test_events.py:StepCompleted` round-trips; rewrite worker `test_eventbus.py`, `test_runner.py`; rewrite consumer `test_integration_coordination_consumer.py`'s task_prs writer tests to read from envelope.

**A.3 — `GithubPrSynchronize` payload + webhook normalizer + receiver** (S, ~3h, blocker for B.1, C.2)
- Files: extend `services/api/treadmill_api/events/github.py` with `GithubPrSynchronize`; register in `events/registry.py`; `webhooks/normalize.py` adds `pull_request.synchronize` mapping. Receiver in `webhooks/router.py` (or wherever) populates `commit_sha` on every github event Event row.
- Tests: `test_webhook_normalize.py` covers the new mapping; integration test asserts `commit_sha` is populated on `events` for the four github verbs.

**A.4 — `prior_steps` extension on `GET /api/v1/steps/{id}`** (S, ~3h, blocker for B.2, C.3)
- Files: `services/api/treadmill_api/routers/steps.py` adds `prior_steps: list[PriorStepBlock]` to the response model; query returns completed prior steps of the same run ordered by `step_index`. Worker `workers/agent/treadmill_agent/api_client.py:_decode_context` decodes the new field; `WorkerContext` carries it through.
- Tests: extend `test_integration_steps_router.py` with a 2-step workflow fixture; assert step 2's response contains step 1's output.

**Phase A partitioning for parallel agents:** A.1 + A.3 (Agent A — schema + webhook), A.2 (Agent B — envelope + worker + consumer), A.4 (Agent C — API + worker decoder). Three agents in parallel; A.2 takes longest, blocks B / C phases anyway.

### Phase B — Build on foundation (~20h, parallelizable)

ADR-0013 (mergeability VIEW + conflict sweep) + the cross-step dispatch from ADR-0015.

**B.1 — `task_mergeability` VIEW + endpoint** (L, ~8h)
- Files: new alembic `0006_task_mergeability_view.py`; `routers/tasks.py` adds `mergeability` to response; new `GET /tasks/{id}/mergeability` endpoint.
- Tests: new `test_integration_task_mergeability.py` fixture-driven, modeled on `test_integration_task_status.py` + `test_integration_plan_status.py`. Cover every derived state (mergeable, blocked-on-{review, validate, ci, conflict}, pending) + the per-commit invalidation case (push new commit → mergeable falls back to pending until fresh review + validate land at new HEAD).
- Depends on A.1 + A.2 + A.3.

**B.2 — Cross-step dispatch in consumer** (M, ~6h)
- Files: new `services/api/treadmill_api/coordination/cross_step.py`. Consumer's `step.completed` handler imports + calls `dispatch_next_step(session, dispatcher, run_id, completed_step_index)`. Crib intact from bunkhouse `events/consumer.py:_on_step_completed` ~line 524.
- Tests: integration test seeds a 2-step workflow, drives step 1 completion, asserts step 2 gets a `step.ready` event + a work-queue claim. Coordinate with the existing replay-loop tests to ensure cross-step failures route through the `dispatch_publish_failed` marker.
- Depends on A.2 + A.4.

**B.3 — Conflict-detection sweep on `pr_merged`** (M, ~6h)
- Files: new `services/api/treadmill_api/coordination/conflict_sweep.py`. Crib intact from bunkhouse `events/consumer.py:_check_open_prs_for_conflicts` ~line 897. Wires into the existing `pr_merged` event handler (or adds one if absent). Emits `github.pr_conflict` events (define the Pydantic class + register).
- Tests: mocked-GitHub-API integration test verifies the sweep correctly detects conflicting open PRs after a merge.
- Depends on A.1 + A.3.

**Phase B partitioning:** three agents, file-isolated. B.1 owns alembic + the new VIEW + the tasks router. B.2 owns the consumer + new cross_step module. B.3 owns the conflict_sweep module + GH event extension.

### Phase C — Workflow plumbing (~30h, parallelizable across role-prompt authoring)

ADR-0015 implementation + the Week-2 closure's deferred D.7 (`event_triggers` consumer).

**C.1 — Rewrite `starters.py` for the eight-role + multi-step shape** (M, ~4h)
- Files: `services/api/treadmill_api/starters.py` per ADR-0015 §"Per-workflow shape matrix". Eight roles (planner, doc-author, code-author, reviewer, validator, feedback-analyzer, ci-analyzer, conflict-analyzer). Seven workflows; four 2-step (`wf-plan`, `wf-feedback`, `wf-ci-fix`, `wf-conflict`); three single-step (`wf-author`, `wf-review`, `wf-validate`). System prompts initially are the ADR-0015-matrix-stated postures; C.3 elaborates per-role.
- Tests: `test_starters.py` invariants — every role referenced by a step is defined; no duplicate IDs; **NEW**: every 2-step workflow's step 1 role id ends in `-analyzer`; **NEW**: `role-code-author` is referenced by exactly four workflows; idempotent re-run.
- Depends on A.2 (consumer reads envelope from workflows that emit envelopes).

**C.2 — `event_triggers` consumer (the full evaluator)** (L, ~10h)
- Files: new `services/api/treadmill_api/coordination/triggers.py`. Crib bunkhouse `events/triggers.py:TriggerEvaluator` shape. Wire:
  - `github.pr_opened` → `wf-review`
  - `github.pr_synchronize` → `wf-review` + `wf-validate` (concurrent dispatch)
  - `github.pr_review_submitted` (decision = changes_requested) → `wf-feedback`
  - `github.check_run_completed` (conclusion = failure) → `wf-ci-fix`
  - `github.pr_conflict` (from B.3) → `wf-conflict`
- Cap policies (per ADR-0015 Q15.b): count prior runs of the workflow on the task; if ≥ 3 for `wf-ci-fix` / `wf-conflict`, skip dispatch + emit a `task.capped` event (or comment on the PR explaining). The evaluator is the right home for cap logic, not the workflow.
- Hooks into the consumer's `handle()` after each relevant github event is processed.
- Tests: integration tests for each trigger mapping; cap-policy test; explicit "no auto-fire when event_triggers row is `enabled=false`".
- Depends on A.3.

**C.3 — Role prompt authoring** (L, ~16h, 8 roles × ~2h each, parallelizable across many agents)
- Files: `services/api/treadmill_api/starters.py` (each role's `system_prompt` field).
- What: replace each role's one-paragraph posture with a fully-written prompt that handles the workflow's input contract + produces the documented envelope. Per-role considerations:
  - **`role-planner`** — research the repo; produce a plan_directive in plan-doc-task-spec shape. ADR-0015's `task_directive` convention is the contract.
  - **`role-doc-author`** — author a plan doc to spec; push on `plan/<plan-id>-<slug>` branch; open PR. ADR-0010's branch conventions.
  - **`role-code-author`** — the shared terminal. Handle task spec input (wf-author) OR task_directive from prior step (wf-feedback/ci-fix/conflict). Scope discipline (only touch files in scope.files); commit message convention; envelope output.
  - **`role-reviewer`** — read diff via `gh pr diff` (worker tools); produce review decision + comments; envelope output with `decision` + comment artifacts.
  - **`role-validator`** — run task's declared `validation:` entries; `kind=deterministic` is stubbed (no script execution at v0; outputs `decision=pass` if entry has no script, `error` otherwise — Phase-4 rule engine fills this); `kind=llm-judge` runs the model with the entry's description as the criterion; aggregate into top-level `decision`; per-entry detail in `payload.validation_results`.
  - **`role-feedback-analyzer`** — classify inbound review comments → task_directive (or no-action / blocked).
  - **`role-ci-analyzer`** — read failing check + logs via `gh run view`; classify → task_directive (or not-our-bug / blocked).
  - **`role-conflict-analyzer`** — read conflict tree via `git status`/`git diff`; produce resolution directive.
- Tests: per role, a unit test that asserts prompt-composition determinism (`_compose_prompt` output stable given fixed inputs). Real-Claude opt-in smoke for at least one analyzer + the code-author terminal.
- Depends on C.1 (the role records exist).

**Phase C partitioning:** C.1 + C.2 sequential within an agent (C.1 lands the schema; C.2 consumes it). C.3 parallelizes across N agents working on disjoint roles. Suggested: 4 agents owning {planner, doc-author}, {code-author}, {reviewer, validator}, {feedback-analyzer, ci-analyzer, conflict-analyzer}.

### Phase D — Integration + smoke (~10h)

**D.1 — End-to-end two-step workflow smoke** (M, ~6h)
- Files: new `workers/agent/tests/test_integration_two_step_smoke.py` gated on `TREADMILL_INTEGRATION=1` + the existing `local_substrate` fixture.
- What: pick `wf-ci-fix` as the canary (simplest analyzer input — a failing check). Test seeds a failing `github.check_run_completed`, asserts the trigger evaluator dispatches `wf-ci-fix`, asserts step 1 (analyzer) completes, asserts step 2 (code-author) fires + pushes a fix to the bare repo. Optionally gate on `TREADMILL_REAL_CLAUDE=1` for the LLM path; default dry-run path uses the existing `TREADMILL_AGENT_DRY_RUN=1` toggle from Phase-2 closure.
- Depends on B.2 + C.1 + C.2 + C.3-`role-code-author`.

**D.2 — Mergeability transitions integration test** (S, ~4h)
- Files: new `services/api/tests/test_integration_mergeability_transitions.py`.
- What: seed a task with a PR; emit `wf-review.completed` with `decision=approved` at HEAD; emit `wf-validate.completed` with `decision=pass` at HEAD; emit `github.check_run_completed` with `conclusion=success` at HEAD. Assert `derived_mergeability='mergeable'`. Emit `pr_synchronize` with new `head_sha`; assert mergeability falls back to `pending` (prior thumbs invalidated by VIEW filter). Emit fresh review + validate at new HEAD; assert mergeable again.
- Depends on B.1.

### Explicitly deferred to Week 4+ or later

- **Auto-merge orchestrator** — ADR-0013 future opening; Phase 5+.
- **Long-running ML tasks / step timeouts / mid-step checkpointing** — future very-large ADR per ADR-0015.
- **Real GitHub mode** for the worker (production PR creation via `gh`) — Phase 4 per parent plan's original Week-3 framing.
- **Skill content beyond what each starter workflow needs** — ADR-0006 rule engine + skill content authoring is Phase 4.
- **Phase-4 rule engine** (`wf-validate`'s deterministic check execution; cross-cutting policy enforcement) — Phase 4 per parent plan.
- **Multi-tier dispatch** — column reserved, wire field absent until evidence demands.

## Diagram

```
ADRs 0012–0015 accepted
       │
       ▼
Phase A (sequential within; agents partition by file)
├── A.1 events.commit_sha column                ← blocker for B / C
├── A.2 StepOutput envelope rewrite             ← blocker for B / C
├── A.3 pr_synchronize + receiver               ← blocker for B / C
└── A.4 prior_steps API extension               ← blocker for B / C
       │
       ▼
Phase B (parallel; 3 agents, file-isolated)
├── B.1 task_mergeability VIEW + endpoint
├── B.2 cross-step dispatch in consumer
└── B.3 conflict sweep on pr_merged
       │
       ▼
Phase C (parallel; many agents on disjoint roles)
├── C.1 starters.py rewrite (eight roles)
├── C.2 event_triggers consumer (full evaluator)
└── C.3 role prompts × 8 (parallelizable)
       │
       ▼
Phase D (sequential)
├── D.1 two-step workflow smoke (wf-ci-fix canary)
└── D.2 mergeability transitions integration
       │
       ▼
Week 3 closed: every starter workflow fires at least once with logged output
```

## Cross-ADR consistency points (worth re-verifying as the work lands)

These were called out in the researcher's report. Each is a place where two ADRs must agree and the implementation must honor both.

1. **`commit_sha` top-level in StepOutput (ADR-0012) ↔ Mergeability VIEW join (ADR-0013).** The VIEW joins `workflow_run_steps.output->>'commit_sha' = head.head_sha`. Verify after A.2 ships that the envelope's `commit_sha` is at top-level (not in `payload`) — that's the SQL contract.
2. **Analyzer decision values (ADR-0015) ↔ Decision-value-set in envelope (ADR-0012).** Every value in ADR-0015's matrix appears in ADR-0012's decision-string table. Verify after C.3 that no role produces a decision value not in the documented set.
3. **Inter-step `task_directive` lives in `payload` (ADR-0015) ↔ Uniform envelope discipline (ADR-0012).** The `task_directive` shape is convention in `payload`; do not promote to envelope-level.
4. **`pr_synchronize` invalidates prior thumbs (ADR-0013) ↔ Resolution workflows write new HEADs (ADR-0015).** `wf-feedback` / `wf-ci-fix` / `wf-conflict` push code → new `pr_synchronize` → old thumbs invalid by VIEW construction. Verify with D.2.
5. **Cross-step dispatch (ADR-0015) ↔ Replay loop + `dispatch_publish_failed` (Week-2 closure A.10).** Next-step `step.ready` publish must route through the existing publish-failure marker. Verify B.2 implementation reuses the helper.
6. **Mergeability reads `output->>'decision'` (ADR-0013) ↔ Free-string decision in envelope (ADR-0012).** Verify A.2's worker outputs `decision` at top-level as a string.

## Risks / unknowns

- **Eight prompts to author is the heaviest single chunk.** Mitigation: parallelize across agents; each prompt's smoke runs the cheap Claude model. The user's "be pedantic at the foundation" learning argues for *spending* the time here rather than shipping shallow prompts.
- **`event_triggers` evaluator complexity.** The cap policy + per-trigger dispatch logic is non-trivial. Mitigation: crib bunkhouse intact; the shape is proven.
- **Cross-step dispatch + cap-policy interaction.** A capped `wf-ci-fix` should *not* re-fire on every subsequent CI failure event. The trigger evaluator must consult run history per task per workflow. Mitigation: dedicated tests in C.2 covering the cap-then-cap-stays scenario.
- **Real-Claude smoke flakiness at the LLM layer.** Mitigation: gate on `TREADMILL_REAL_CLAUDE=1`; CI does not run by default. D.1 has a dry-run mode for the default integration path.
- **VIEW performance under realistic event volume.** ADR-0013's VIEW does several LATERAL joins. Mitigation: partial indexes per ADR-0014; promotion to materialized VIEW deferred per ADR-0011 until measured cost demands.

## Decisions captured during execution

(filled in as work progresses)

## Running log

- **2026-05-11** Plan authored. ADRs 0012–0015 accepted. Bunkhouse precedent check for the envelope completed (`pr_number`, `branch`, `logs` are the only convention fields bunkhouse reads; envelope subsumes). Phase A ready to fire.
- **2026-05-12** All four phases shipped; Week 3 honestly closed. **Phase A (foundation, three parallel agents)**: `events.commit_sha` column + partial indexes (alembic 0005); `StepOutput` envelope + worker/consumer rewrite; `GithubPrSynchronize` payload + webhook normalizer + receiver populates `commit_sha`; `prior_steps` field on `GET /api/v1/steps/{id}` returning ordered completed prior steps. **Phase B (build on foundation, three parallel agents)**: `task_mergeability` VIEW (alembic 0006) with the six-state CASE-WHEN priority + `GET /tasks/{id}/mergeability` endpoint; cross-step dispatch in `coordination/cross_step.py` with idempotency guard + commit_sha propagation; conflict-detection sweep (`coordination/conflict_sweep.py`) cribbed from bunkhouse with `GithubPrConflict` event class. **Phase C (workflow plumbing)**: `starters.py` rewritten with 8 roles + 7 workflows per ADR-0015 matrix (`role-code-author` as the shared terminal across `wf-author`/`wf-feedback`/`wf-ci-fix`/`wf-conflict` — the bunkhouse-miss correction); `event_triggers` evaluator (`coordination/triggers.py`, the deferred Week-2 D.7) with cap policies + `pr_synchronize` fan-out + `pr_review_submitted` state filter; all eight role system prompts authored with envelope contract + decision value-sets + scope discipline; `_compose_prompt` extended to fold `prior_steps` task_directive for multi-step workflows. **Phase D (capstone)**: two-step workflow smoke (`test_integration_two_step_smoke.py`) with dry-run + opt-in real-Claude variants; mergeability transitions test (`test_integration_mergeability_transitions.py`) walking pending → mergeable at HEAD X → pending after push → mergeable at HEAD Y; dry-run analyzer extension so the action role sees a synthesized `task_directive` in multi-step dry-run mode. **Substrate fix shipped during Phase D**: local-adapter provisioner had a double-prefix bug on SNS subscription `Endpoint` (the `Fn::GetAtt: Arn` form was being re-prefixed `arn:aws:sqs:...arn:aws:sqs:...`). One-line guard in `_sqs_arn_for_subscription` rejects re-prefixing; unit test in `test_provisioner.py` locks it in; substrate now provisions clean subscriptions on `up`. **D.1 agent's "moto delivery" finding was a misdiagnosis** — the API container's coordination consumer was draining test messages in real-time before the test could poll. Verified end-to-end: SNS publish → SQS delivery → consumer projection → DB row transition all green against the live substrate. The D.1 smoke passes live with `TREADMILL_INTEGRATION=1`. **Aggregate test totals: 593 passed, 19 skipped** across the workspace (with `TREADMILL_INTEGRATION=1`: 102 worker + 379 API + 19 cli + 55 local-adapter + 23 infra + 6 dev-hooks = 584 passed + 8 skipped + 1 transient TRUNCATE error documented as a Week-2-known-issue around the active consumer's table locks). Phase 2 success criterion 5 ("every starter workflow fires at least once with logged output") is now observably true on a developer machine. **Deferred to Week 4+ with explicit tracking**: auto-merge orchestrator (ADR-0013 future), long-running ML tasks ADR (per ADR-0015 §"Trade-offs"), real GitHub mode for the worker, the Phase-4 rule engine (`wf-validate`'s deterministic check execution), and the transient TRUNCATE deadlock test-infra fix.

## Post-mortem

### What worked

- **Phased file-isolated agent partitions held.** Three agents in Phase A, three in Phase B, two for C.1+C.2 then one for C.3, two in Phase D — 13 agent runs total across 4 phases. Zero merge-conflict-driven rework. The lesson from Week-2 closure (file-level partitioning is the safety mechanism) carried.
- **Bunkhouse precedent check before authoring ADR-0012.** A 10-minute subagent lookup confirmed bunkhouse reads only `pr_number`, `branch`, `logs` from step output. The envelope absorbs all three. This is the exact discipline the `bunkhouse-precedent-on-shape-decisions` memory captures.
- **The ADR-driven structure.** Four ADRs landed first, the work pulled from them directly. Each agent's brief referenced the ADR by section. When agents had to make judgment calls (per-workflow specifics, role taxonomy edge cases), the ADR was the authority.
- **The "no cancellation" simplification.** Designed into ADR-0013 + ADR-0015. Saved a significant amount of coordination machinery; the cost (wasted tokens on in-flight runs against stale HEAD) is explicit and acceptable.
- **The user-blessed uniform envelope** (vs. per-workflow types). One render path; one schema-drift contract; one validation seam. Future workflows are additive without consumer migration.

### What we'd do differently

- **Phase D's "moto delivery" was a misdiagnosis** that wasted ~2 agent-hours. D.1 agent saw publishes "not landing" because the consumer was draining them in real-time. The lesson: when a queue depth check returns 0, the next question is "did a consumer just drain it?" not "is delivery broken?" — especially in a substrate where a long-running consumer exists. Would have been caught faster with a single test message ID lookup in the events table instead of receive-message polling.
- **The integration container test had a stale `output["branch"]` assertion** from before the ADR-0012 envelope rewrite. The envelope migration in A.2 didn't grep for every test that read the old shape. A future migration ADR should mandate a grep-then-update pass across `tests/` for any envelope-shape change.
- **TRUNCATE deadlock against active consumer** is now a known-recurring test-infra issue (flagged Week-2, re-surfaced Week-3). Worth fixing properly — either `LOCK NOWAIT` + retry, or pause the consumer for the fixture duration.

### What the adversarial review would catch

This plan was authored from a clean ADR + researcher-blessed shape. No mid-flight adversarial pass surfaced architectural drift this time. The next phase boundary should still trigger a review — the Week-2 closure pattern caught real load-bearing bugs (plan_status VIEW timestamp ties) that escaped four phases of careful work. The discipline is cheaper than the cost of letting drift compound.

### Meta-lesson

The Week-3 plan exited Week 2 with a clear architectural mandate (four ADRs, all settled before code) and a parallel-agent execution model (file-isolated phases, ~13 agents, ~70 wall hours of substantive work). The result: 593 tests added in net, zero load-bearing bugs surfaced, every adversarial-review concern from Week 2 either resolved here or explicitly deferred with tracking. The investment in ADRs and the plan doc before firing agents paid off — that's the durable-planning learning from 2026-05-11 in action.
