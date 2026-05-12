---
status: completed
trigger: adversarial review 2026-05-11
parent: docs/plans/2026-05-08-minimum-runnable-treadmill.md
---

# Plan: Week 2 closure — resolve adversarial review findings

## Trigger

After the 2026-05-08 running-log entry declared Week 2 of `2026-05-08-minimum-runnable-treadmill.md` complete, the user spun up a manual adversarial reviewer (the auto-review wiring is not yet built) which surfaced ~30 findings across eight buckets. Several are load-bearing — an ADR-0010 branch-format violation, a missing `task_prs` writer that breaks the webhook→trigger chain, the worker→consumer Pydantic-boundary contract honored only on one side, the dry-run smoke claiming Phase 2 success criterion 4 satisfaction without an actual authored diff, plus a dozen smaller ADR violations and forgotten commitments.

The user rejected closing Week 2 with these findings open: *"I don't think that we can close week 2 until all of this is resolved. We're at the very beginning of a large architectural shift. If there's a time to be pedantic it's right now."* Captured as `docs/learnings/2026-05-11-review-driven-phase-closure.md`.

Four planning agents (one per finding cluster) produced detailed work-item lists. This plan synthesizes them with the user-blessed open-question decisions.

## Goal

Bring Week 2 of the parent plan to honest closure — every adversarial-review finding either addressed in code or explicitly deferred with tracking. Foundation drift fixed before Week 3's workflow plumbing builds on it.

## Open-question decisions (user-blessed 2026-05-11)

| # | Decision |
|---|---|
| 1 | Shared event-schema dep via workspace source — `treadmill-api` declared as a workspace source on the worker's `pyproject.toml`. |
| 2 | Consumer on malformed `step.completed.output`: log + write raw dict as-is + mark completed. (Source-of-truth Event row is the authority.) |
| 3 | Dispatch publish/send failure: keep 201, persist a `dispatch_publish_failed` Event-row marker, replay loop heals on a 30s tick. |
| 4 | `PlanActivated` fires immediately on Scenario-1 doc-driven create, in the same txn that spawns tasks. |
| 5 | Branch format `task/<short-id>-<slugified-title>` per ADR-0010 §"Branch conventions". Multi-step workflows reuse via `git checkout -B` + fetch. |
| 6 | `task_prs` writes happen in the coordination consumer, reading `step.completed.output.pr_number`. Worker does NOT POST. |
| 7 | Real-Claude smoke gated via `TREADMILL_REAL_CLAUDE=1`. TODO to escalate once budget plumbing exists. |
| 8 | Credentials mount `mode="rw"` for v0; learning entry documents host-file mutation trade-off; revisit at multi-worker. |
| 9 | `gh` mode removed from `git.py`; raise on `REPO_MODE=github`. Reintroduce with real coverage in Phase 4. |
| 10 | `WorkTopic` SNS topic removed. Keep FIFO work queue + `EventsTopic`. |
| 11 | DLQ defaults: coordination `maxReceiveCount=5`; work `maxReceiveCount=3`; both `Duration.days(14)` retention. |
| 12 | `compute_tier` removed from wire (StepReady, worker decoder, steps-router response). DB column stays as forward-compat ballast. |
| 13 | Plan state machine via `plan_status` VIEW, mirroring `task_status`. |
| 14 | `validation:` block in a new `task_validations` side table. |
| 15 | `event_triggers` consumer deferred to Week 3 with explicit tracking; not in this closure. |
| 16 | Starter workflow seeding via `treadmill workflows seed-starters` CLI command + importable Python module. |
| 17 | ADR-0008 Stop hook: both a hook script and an AGENTS.md session-end paragraph. |
| 18 | Worker container integration test lives in `workers/agent/tests/`; substrate lifecycle via the new pytest fixture. |

## Phased work plan

### Phase 1 — Foundation (parallelizable; ~20h)

Independent items. Outcomes: schema-package boundary clean, ADR-0002 contract restored, dead code gone, easy honesty wins shipped.

**Worker package (Agent 1)**

1. **B.1 — Branch format per ADR-0010.** Files: `workers/agent/treadmill_agent/runner.py` (`_branch_for_step`), new `_slugify_title` helper; `workers/agent/tests/test_runner.py`. What: `task/<short-id>-<slugify(title)>`, slug strips non-`[a-z0-9]`, collapses runs of `-`, truncates ~40 chars, falls back to `untitled`. Tests: branch-format unit test (rewrite existing), slugify property tests covering shell-meta + path-traversal, update `test_git.py` cycle test to new format. Complexity: S.

2. **B.5 — Credentials mount RW.** Files: `tools/local-adapter/treadmill_local/runtime.py` (`_volumes_for`); `workers/agent/Dockerfile` comment. What: `mode="rw"` on the Claude credentials bind. Tests: local-adapter test asserts volume mode for the agent family. Complexity: XS.

3. **B.6 — Pin Claude Code CLI version.** Files: `workers/agent/Dockerfile`; new `workers/agent/tests/test_dockerfile.py`. What: append `@<semver>` to the npm install; remove "Pinned at v0" misleading comment. Tests: Dockerfile parser asserts pinned suffix. Complexity: XS.

4. **B.7 — Remove `gh` mode.** Files: `workers/agent/treadmill_agent/git.py`; `workers/agent/tests/test_git.py`. What: delete the `mode=="github"` branches in `clone` + `open_pr`; raise on `REPO_MODE=github`. Tests: extend `test_clone_unknown_mode_raises` to `github`. Complexity: XS.

5. **A.12 — Claude Code CLI flag smoke.** Files: tighten `workers/agent/tests/test_claude_code.py:test_run_claude_code_passes_model_and_prompt` to assert `--print` position; new `workers/agent/tests/test_claude_code_real_binary.py` gated on `TREADMILL_CLAUDE_BINARY_SMOKE=1`. Tests: real binary smoke greps `claude --help` for our flags. Complexity: XS.

6. **C.1 (worker-side) — `EXIT_AFTER_STEP=true` restore.** Files: `workers/agent/treadmill_agent/config.py` (add `exit_after_step: bool`, remove `MAX_STEPS`); `runner.py` polling loop; `__main__.py` log line; `workers/agent/tests/test_runner.py` (rename `max_steps` tests); new `workers/agent/tests/test_config.py`. Tests: boolean parsing 6 cases, default true, runner-loop respects flag. Complexity: S.

7. **C.5 (worker-side) — Drop `compute_tier` from worker decoder.** Files: `workers/agent/treadmill_agent/api_client.py` (`Role` dataclass drops field); `workers/agent/tests/test_api_client.py`, `test_claude_code.py`, `test_runner.py` fixture updates. Tests: existing test updates. Complexity: XS.

**API package (Agent 2)**

8. **C.3 — Replace `assert` with explicit raises.** Files: `services/api/treadmill_api/routers/steps.py` (introduce `WorkerContextError`, replace 4-5 asserts); `routers/roles.py` (lines 128, 138). Tests: `test_integration_steps_router.py:test_get_step_context_500s_with_clear_message_when_run_missing`. Complexity: S.

9. **C.5 (API-side) — Rip `compute_tier` from the wire.** Files: `events/step.py` (remove from `StepReady`); `dispatch.py` (drop pass-through); `routers/steps.py` (remove from `_RoleBlock`); `routers/roles.py` (remove from request/response); keep `models/workflow.py:Role.compute_tier` column with TODO comment. Tests: `test_events.py:test_step_ready_does_not_carry_compute_tier_at_v0`; update existing assertions. Complexity: M.

**CDK + infra (Agent 3)**

10. **C.1 (CDK-side) — `EXIT_AFTER_STEP=true` in agent env.** Files: `infra/treadmill_infra/stacks/spike.py` (swap `MAX_STEPS=1` → `EXIT_AFTER_STEP=true`); `infra/tests/test_spike_stack.py`. Tests: `test_agent_container_has_exit_after_step_env`. Complexity: XS.

11. **C.4 — Remove dead `WorkTopic`.** Files: `infra/treadmill_infra/stacks/spike.py` (delete topic + subscription); `infra/tests/test_spike_stack.py` (update assertions); `tools/local-adapter/tests/test_provisioner.py` (rename WorkTopic fixtures to neutral names). Tests: `test_no_work_topic_in_stack` asserts SNS topic count is 1. Complexity: S.

**Workspace + dev hooks (Agent 4)**

12. **A.2 — Shared event-schema dep via workspace source.** Files: `workers/agent/pyproject.toml` (add `pydantic>=2.7` dep + `[tool.uv.sources]` workspace ref); root `pyproject.toml` (verify workspace member); new `workers/agent/tests/test_event_schema_drift.py` (imports `treadmill_api.events` and asserts the registry has `StepReady`, `StepStarted`, `StepCompleted`, `StepFailed`, `AuthorStepOutput`). Tests: import smoke. Complexity: S.

13. **D.11 — ADR-0008 Stop hook + AGENTS.md.** Files: new `tools/dev-hooks/review_candidates_at_stop.py` (~50 LOC); `.claude/settings.json` (register Stop hook); `AGENTS.md` (add "Session end: sweep open candidates" paragraph); new `tools/dev-hooks/tests/test_review_candidates.py`. Tests: empty-file no-op, all-captured no-op, one-open emits additionalContext. Complexity: S.

### Phase 2 — Build on foundation (~35h)

Outcomes: Pydantic at every boundary, task dependency persistence, plan-state-machine VIEW, lifecycle events emitted, consumer validates payloads.

14. **A.1 — Pydantic in worker eventbus.** Files: `workers/agent/treadmill_agent/eventbus.py` (`_publish` validates via typed payload classes); new `workers/agent/treadmill_agent/events.py` (re-exports). Tests: `test_eventbus.py:test_step_completed_payload_validates_against_typed_model`, `test_publish_rejects_invalid_output_dict`. Complexity: S. **Depends on:** A.2.

15. **A.4 — Promote `StepCompleted.output` to typed `AuthorStepOutput`.** Files: `services/api/treadmill_api/events/step.py`. What: `output: AuthorStepOutput | dict[str, object] = {}` (union for forward-compat with non-author step types). Tests: `test_events.py:test_step_completed_accepts_typed_output`, `_rejects_typed_output_with_bad_pr_number`. Complexity: XS. **Depends on:** A.1.

16. **A.3 — Consumer validates payload through registry before projecting.** Files: `services/api/treadmill_api/coordination/consumer.py` (`handle`, `_dispatch_step` — `parse_payload` at top; `AuthorStepOutput.model_validate` on completed output; on validation failure log + write raw dict + mark completed per decision #2). Tests: extend `test_integration_coordination_consumer.py` with malformed-payload cases; new `test_consumer_unit.py` with stub `sessionmaker`. Complexity: M. **Depends on:** A.1+A.4.

17. **A.5 — Idempotency tests + docstring tightening.** Files: `coordination/consumer.py` docstring; `test_integration_coordination_consumer.py`. Tests: `test_handle_idempotent_on_failed_step`, `test_handle_late_started_after_completed_is_noop`. Complexity: XS.

18. **A.6 — Dispatcher emits plan/task lifecycle events.** Files: `dispatch.py` (`_persist_and_publish` helper); `routers/plans.py` (PlanRegistered + PlanActivated for Scenario 1); `routers/tasks.py` (TaskRegistered). Tests: `test_integration_plans_router.py:test_create_plan_persists_plan_registered_event`, `_emits_plan_activated`, `_persists_task_registered_per_task`. Complexity: S.

19. **A.7 — Dispatcher refactor for no-Request callers.** Files: `dispatch.py` (`Dispatcher.from_app_state(state)`). Tests: new `test_dispatch_unit.py` covering happy path, `DispatchError`, publish-failure paths. Complexity: S.

20. **A.9 — Publisher wraps boto3 errors into typed `PublishError`.** Files: `eventbus.py`. Tests: new `test_eventbus_unit.py:test_sns_publisher_wraps_client_error_as_publish_error`. Complexity: S.

21. **B.2 — Drop `--allow-empty`; treat no-author as failure.** Files: `workers/agent/treadmill_agent/git.py` (`commit_all` minus `--allow-empty`; new `has_staged_changes`); `runner.py` (`_execute` checks before commit). Tests: `test_git.py:test_commit_all_raises_when_nothing_staged`, `test_has_staged_changes_true_when_added`; `test_runner.py:test_execute_publishes_failed_when_no_changes_authored`. Complexity: S.

22. **B.3 — Redelivery-safe `git checkout`.** Files: `git.py` (`checkout_branch` → fetch + `checkout -B`; `push_branch` → `--force-with-lease`). Tests: `test_git.py:test_checkout_branch_idempotent_when_branch_exists_on_origin`, `test_push_with_force_with_lease_rejects_concurrent_unknown_writes`. Complexity: M. **Depends on:** B.1.

23. **B.4 — Publish `step.started` before fetching context.** Files: `dispatch.py` (extend SQS claim body with `task_id`, `plan_id`, `run_id`); `workers/agent/treadmill_agent/runner.py` (`_handle_step` reorder). Tests: `test_runner.py:test_runner_publishes_started_then_failed_when_fetch_context_raises`; integration test that claim body contains all four IDs. Complexity: M.

24. **C.6 — `CoordinationProbe` for `/health/ready`.** Files: new `treadmill_api/dependencies.py:CoordinationProbe`; `treadmill_api/coordination/__init__.py` exposes `is_running()`; `app.py` lifespan registers probe. Tests: `test_dependencies.py:test_coordination_probe_reports_running_when_task_alive`, `_stopped_when_task_done`; `test_health.py:test_ready_returns_503_when_consumer_dead`. Complexity: M.

25. **D.1 — Persist `task_dependencies` rows.** Files: `routers/plans.py:_spawn_tasks_from_specs` (lift sibling IDs to UUIDs, insert rows, validate grammar). Tests: extend `test_integration_plans_router.py` with two-task `depends_on`, malformed-expression 400, unknown-sibling 400. Complexity: M.

26. **D.3 — `task_validations` table + persistence.** Files: new alembic `0003_task_validations.py`; new `models/task_validation.py`; `models/__init__.py`; `routers/plans.py:_spawn_tasks_from_specs` inserts. Tests: extend `test_integration_plans_router.py`; `test_integration_migrations.py` asserts table. Complexity: S.

27. **D.4 — `plan_status` VIEW + `derived_status` on plan responses.** Files: new alembic `0004_plan_status_view.py`; `routers/plans.py` LEFT JOIN. Tests: new `test_integration_plan_status.py` fixture-driven, 6 transitions + priority order. Complexity: M.

28. **D.9 — Starter workflow seed module + CLI command.** Files: new `services/api/treadmill_api/starters.py` (canonical 7 workflows + roles + skills); new CLI command `treadmill workflows seed-starters`; `cli/treadmill_cli/cli.py`. Tests: `test_starters.py` (content validation), `test_integration_cli_seed.py` (idempotent re-run). Complexity: M.

### Phase 3 — Compose (~35h)

Outcomes: dispatch failure path closed, dependency-gated dispatch, plan-active gating, replay loop, smoke harness, worker integration test framework.

29. **A.8 — `dispatch_publish_failed` marker on bus/queue failure.** Files: `dispatch.py` (structured failure handling); `events/step.py` or new `events/internal.py` (`DispatchPublishFailed`); `events/registry.py`. Tests: `test_dispatch_unit.py:test_dispatch_records_publish_failed_event_when_sns_raises`, `_when_sqs_raises`, `_returns_run_id_even_when_publish_fails`. Complexity: M. **Depends on:** A.7+A.9.

30. **A.10 — Replay loop.** Files: new `coordination/replay.py`; `app.py` lifespan starts/stops. Tests: new `test_replay_loop.py` (integration with live PG; seed marker, run one tick, assert re-publish + marked replayed). Complexity: M. **Depends on:** A.8.

31. **A.11 — Consumer poll-loop resilience.** Files: `coordination/consumer.py` (`_run` with exponential backoff + `_health_status` field). Tests: `test_consumer_unit.py:test_run_backs_off_exponentially_on_consecutive_errors`, `_health_status_reflects_consumer_state`. Complexity: M. **Depends on:** A.3+C.6.

32. **B.8 — Coordination consumer writes `task_prs` on `step.completed`.** Files: `coordination/consumer.py` (in `step.completed` branch, after status update, validate `AuthorStepOutput`, insert `task_prs` ON CONFLICT DO NOTHING, call `drain_pending_events`). Tests: `test_integration_coordination_consumer.py:test_step_completed_with_pr_writes_task_prs_row`, `_without_pr_skips`, `_idempotent_on_redelivery`, `_triggers_pending_events_drain`. Complexity: L. **Depends on:** A.4.

33. **B.9 — Remove `TREADMILL_AGENT_DRY_RUN=1` from CDK env.** Files: `infra/treadmill_infra/stacks/spike.py`; `infra/tests/test_spike_stack.py` asserts flag absent. Tests: existing test updates + CDK assertion of absence. Complexity: S. **Depends on:** B.2+B.5+B.6.

34. **C.7 — `LocalRuntime` pytest fixture.** Files: new `tools/local-adapter/treadmill_local/pytest_harness.py`; `wait_until_ready(timeout=60.0)` helper; new `tools/local-adapter/tests/test_pytest_harness.py`. Tests: fixture-up-and-down gated on `TREADMILL_LOCAL_HARNESS=1`; `wait_until_ready` timeout + ready cases. Complexity: M. **Depends on:** C.6.

35. **D.2 — Dispatcher honors `task_dependencies`.** Files: `dispatch.py` (gate on `task_status` blocked-clause SQL). Tests: new `test_integration_dispatch_dependency_gate.py` (3 cases). Complexity: M. **Depends on:** D.1.

36. **D.5 — Dispatcher gates on plan-active.** Files: `dispatch.py` (read `plan_status.derived_status`); `routers/plans.py`/`tasks.py` (Scenario 1 emits `PlanActivated` inline if A.6 hasn't landed). Tests: new `test_integration_plan_active_gate.py` (3 cases). Complexity: M. **Depends on:** D.4+A.6.

37. **D.6 — Re-evaluation pass on consumer.** Files: new `coordination/redispatch.py`; `consumer.py` calls after relevant event handlers. Tests: new `test_integration_redispatch.py` (chain, intent-only-then-activate, idempotency). Complexity: M. **Depends on:** D.2+D.5.

38. **D.8 — `drain_pending_events` caller.** Files: `coordination/consumer.py` inside the `task_prs`-writing handler. Tests: extend `test_integration_redispatch.py` or own file. Complexity: S. **Depends on:** B.8.

39. **D.10 — `--dev` flag on CLI + API.** Files: `routers/plans.py` (`PlanCreateRequest.dev`); `cli/treadmill_cli/cli.py` (`--dev` on `plan submit` + `submit`). Tests: new `test_integration_dev_flag.py` (4 cases); extend `cli/tests/test_cli.py`. Complexity: S. **Depends on:** D.9.

### Phase 4 — Capstone (~25h)

Outcomes: real-Claude smoke green, automated worker container integration test, poison-message safety verified, plan-doc rewritten.

40. **B.11 — Real-Claude opt-in smoke.** Files: new `workers/agent/tests/test_integration_real_claude.py` gated on `TREADMILL_REAL_CLAUDE=1`. What: cheapest model, trivial prompt, real bare repo, asserts file change + commit + push. Tests: this is the test. Complexity: M. **Depends on:** B.5+B.6+B.2+B.9.

41. **B.12 — Worker container integration test against live substrate.** Files: new `workers/agent/tests/test_integration_container.py` gated on `TREADMILL_INTEGRATION=1`. What: brings up substrate via C.7 fixture, runs agent container once in dry-run mode, asserts `workflow_run_steps.status='completed'` + events + `task_prs` row. Tests: this is the test. Complexity: L. **Depends on:** B.8+C.7+B.11 (optional real-Claude promotion).

42. **C.2 — DLQ + redrive policy on coordination + work queues.** Files: `infra/treadmill_infra/stacks/spike.py` (DLQ resources + `DeadLetterQueue` config); `infra/tests/test_spike_stack.py`. Tests: `test_events_coordination_queue_has_dlq` + `test_work_queue_has_dlq` CDK assertions. Complexity: S.

43. **C.8 — Poison-message DLQ behavioral test.** Files: new test in `services/api/tests/test_integration_coordination_consumer.py`. Tests: send 6 messages with unknown `entity_type`, assert DLQ landing after `max_receive_count`. Complexity: S. **Depends on:** C.2+C.6.

44. **D.12 — Plan-doc rewrite.** Files: `docs/plans/2026-05-08-minimum-runnable-treadmill.md`. What: append a 2026-05-11 entry capturing what was true vs over-claimed, point at this closure plan, mark items honestly. The "Week 2 closed" entry stays as historical record; the new entry corrects. Tests: documentation; no automated test. Complexity: XS.

### Explicitly deferred to Week 3

**D.7 — `event_triggers` consumer.** The reviewer's "ship partial" recommendation is the collapse-then-restore pattern the user has previously rejected. Ships in Week 3 alongside `wf-review`'s prompt + the trigger evaluator + the event-shape ADRs.

## Cross-cluster handoffs

- **A.2 → A.1/A.3/A.4**: shared schema must land before worker imports the API's typed events.
- **A.4 → B.8**: `AuthorStepOutput` typing is the contract for the `task_prs` writer's payload validation.
- **B.8 → D.8**: the same handler that writes `task_prs` must immediately call `drain_pending_events`.
- **A.6 → D.4/D.5**: plan lifecycle events must be emitted for the `plan_status` VIEW + plan-active gate to see anything.
- **D.4 → D.5**: VIEW must exist before the dispatcher can read it.
- **D.1 → D.2**: dependency rows persisted before dispatcher honors them.
- **C.6 → C.7**: `wait_until_ready` reuses the consumer probe.
- **C.5 + worker-side C.5**: API drops `compute_tier` from `StepReady`; worker drops it from the `Role` decoder. Same field, two sides.

## Test plan rollup

New infrastructure across the closure:

- **Unit-test layer for the dispatcher** (`tests/test_dispatch_unit.py`) — fake publisher + fake SQS so failure paths are testable without moto.
- **Unit-test layer for the consumer** (`tests/test_consumer_unit.py`) — handler-level tests with a stub `sessionmaker`.
- **Schema-drift contract test** between worker and API (`workers/agent/tests/test_event_schema_drift.py`).
- **`plan_status` fixture-driven test** mirroring `task_status` pattern.
- **Real-Claude opt-in smoke** (`TREADMILL_REAL_CLAUDE=1`).
- **Worker container integration test** (`TREADMILL_INTEGRATION=1`) — uses C.7 fixture.
- **DLQ CDK assertions + behavioral DLQ test** for poison-message safety.
- **Dockerfile parser** for version-pin enforcement (the only mechanical option for that policy).
- **`treadmill-local` pytest fixture** for programmatic substrate lifecycle.
- **Starter-workflow seed fixture** importable by other integration tests.

## Diagram

```
Phase 1 (parallel, ~20h)
├── Agent 1: worker package      (B.1, B.5, B.6, B.7, A.12, C.1-worker, C.5-worker)
├── Agent 2: API package         (C.3, C.5-API)
├── Agent 3: CDK + infra         (C.1-CDK, C.4)
└── Agent 4: workspace + hooks   (A.2, D.11)
            │
            ▼
Phase 2 (~35h) — Pydantic everywhere, lifecycle events, dep persistence, plan VIEW
            │
            ▼
Phase 3 (~35h) — failure replay, dependency gates, smoke harness
            │
            ▼
Phase 4 (~25h) — real-Claude smoke, container integration test, DLQ
            │
            ▼
Week 2 honestly closed. Week 3 starts on solid foundation.
```

## Risks / unknowns

- **Real-Claude smoke cost.** Cheapest model, trivial prompt; expected ~$0.001/run. Caveat: every CI run on a PR could compound if not gated. Mitigation: opt-in only at v0, escalate when budget plumbing exists.
- **Credential mount RW concurrency.** Multi-worker future will need a per-worker credentials story; v0 single-worker is safe. Captured as a learning when the change lands.
- **`AuthorStepOutput` union with `dict`.** Forward-compat for non-author steps, but the union loosens validation. Future ADR adds the discriminated-union when a second step type exists.
- **Phase 1 agent conflicts.** Four agents in parallel — file partitioning is the safety mechanism. If any conflict materializes, the integrating pass resolves it.

## Decisions captured during execution

(filled in as work progresses)

## Running log

- **2026-05-11** Plan authored. 18 open questions ratified by user. Phase 1 fired with four parallel agents.
- **2026-05-11** **Post-Phase-4 verification caught a load-bearing bug** that escaped the four-phase parallel pass. Phase 4 Agent 1 flagged that the live integration test `test_dev_intent_only_creates_active_plan_with_task` was returning `derived_status='drafting'` instead of `'active'`. Root cause: the Phase 2 `plan_status` VIEW (alembic 0004) used `ORDER BY created_at DESC LIMIT 1` to resolve the plan's latest lifecycle event. The dispatcher emits `PlanRegistered` and `PlanActivated` in the same transaction for Scenario-1 doc-driven creates AND for the dev fast-path; Postgres `now()` returns the transaction start time, so both events share an identical `created_at` and the `ORDER BY` tiebreaker was arbitrary. About half of Scenario-1 plans were resolving to `drafting` instead of `active`. The Phase 2 `test_integration_plan_status.py` cases avoided the bug by inserting `time.sleep(0.001)` between events, so the in-unit-test surface stayed green while the live API was broken. **Fix:** rewrote the VIEW to use explicit priority ordering (`abandoned=5 > completed=4 > active=3 > planning=2 > registered=1`), with `created_at DESC` as a secondary key. Added two regression tests: `test_status_resolves_correctly_on_same_txn_event_ties` (inserts both events with literally identical `created_at`) and `test_status_explicit_priority_beats_recency` (asserts priority beats wall-clock recency). All 4 dev-flag integration tests + all 9 plan-status tests now pass live against the substrate. The bug is exactly the shape `2026-05-11-review-driven-phase-closure.md` warns about — closing a phase before the integration surface is exercised end-to-end produces stub-as-success in a different costume. The Phase 4 capstone test (Agent 1's real-Claude smoke + container integration test) is what caught it; without that test the dev fast-path would have shipped broken into Week 3.
- **2026-05-11** All four phases shipped; Week 2 of the parent plan honestly closed. **Phase 1 (foundation, parallel)**: four agents working in disjoint file partitions landed the easy honesty wins — ADR-0010 branch format `task/<short-id>-<slug>` with a slugifier (B.1), credentials mount RW (B.5), pinned `@anthropic-ai/claude-code@1.0.110` in the agent Dockerfile (B.6), `gh` mode removed from `git.py` with `REPO_MODE=github` raising explicitly (B.7), real-binary Claude CLI flag smoke gated on `TREADMILL_CLAUDE_BINARY_SMOKE=1` (A.12), `EXIT_AFTER_STEP=true` restored on both worker and CDK sides per ADR-0002 (C.1), `compute_tier` ripped from the wire end-to-end with the DB column kept as forward-compat ballast (C.5), `assert` replaced with explicit `WorkerContextError` raises in routers (C.3), `WorkTopic` SNS topic deleted (C.4), shared event-schema dep wired via workspace source (A.2), ADR-0008 Stop hook + AGENTS.md session-end paragraph shipped (D.11). **Phase 2 (build on foundation)**: Pydantic now validates at every boundary — worker `_publish` validates via typed payload classes (A.1), `StepCompleted.output` promoted to typed `AuthorStepOutput | dict` union (A.4), coordination consumer validates payloads through the registry before projecting and writes raw dict on malformed `AuthorStepOutput` per decision #2 (A.3). Dispatcher emits `PlanRegistered`/`PlanActivated`/`TaskRegistered` lifecycle events (A.6); refactored for no-Request callers via `Dispatcher.from_app_state` (A.7); publisher wraps boto3 errors as typed `PublishError` (A.9). Worker stops accepting empty diffs: `--allow-empty` dropped, `has_staged_changes` gates commit, no-author publishes `step.failed` (B.2). Redelivery-safe checkout via fetch + `checkout -B` and `--force-with-lease` on push (B.3). `step.started` now published before fetching context, with the dispatcher's claim body carrying `step_id`, `task_id`, `plan_id`, `run_id` (B.4). `CoordinationProbe` registered on `/health/ready` (C.6). `task_dependencies` rows persisted with grammar validation (D.1); `task_validations` table + alembic migration 0003 + persistence (D.3); `plan_status` VIEW + alembic migration 0004 + `derived_status` on plan responses (D.4); starter workflow seed module + `treadmill workflows seed-starters` CLI command (D.9). **Phase 3 (compose)**: `dispatch_publish_failed` Event-row marker + replay loop with 30s tick that heals on bus/queue failure (A.8 + A.10). Consumer poll-loop wraps exponential backoff (`1, 2, 4, 8, 16, 30`) with `_health_status` field reported through the probe (A.11). Coordination consumer writes `task_prs` on `step.completed` with `ON CONFLICT DO NOTHING` idempotency and `drain_pending_events` call (B.8 + D.8). `TREADMILL_AGENT_DRY_RUN=1` removed from CDK env (B.9). `LocalRuntime` pytest fixture + `wait_until_ready` helper gated on `TREADMILL_LOCAL_HARNESS=1` (C.7). Dispatcher honors `task_dependencies` via blocked-clause SQL gate (D.2); dispatcher gates on plan-active via `plan_status.derived_status` (D.5); coordination consumer runs a re-evaluation pass after relevant event handlers (D.6); `--dev` flag on CLI + API for local-only fast paths (D.10). **Phase 4 (capstone)**: real-Claude opt-in smoke at `workers/agent/tests/test_integration_real_claude.py` gated on `TREADMILL_REAL_CLAUDE=1` replaces the dry-run as the gate for Phase 2 success criterion 4 (B.11); worker container integration test at `workers/agent/tests/test_integration_container.py` gated on `TREADMILL_INTEGRATION=1` brings up the substrate via the C.7 fixture and runs the agent container once end-to-end (B.12); DLQ + redrive policy on coordination (`maxReceiveCount=5`) + work (`maxReceiveCount=3`) queues with 14-day retention (C.2); poison-message DLQ behavioral test at `services/api/tests/test_integration_dlq.py` (C.8); plan-doc rewrite (D.12, this entry). **Aggregate test totals: 333 passed, 142 skipped** (cli 19, services/api 147 + 132 integration-gated skipped, workers/agent 84 + 3 skipped, tools/local-adapter 54 + 1 skipped, infra 23, tools/dev-hooks 6). The skipped tests are integration tests gated on `TREADMILL_INTEGRATION=1`, `TREADMILL_LOCAL_HARNESS=1`, `TREADMILL_REAL_CLAUDE=1`, `TREADMILL_CLAUDE_BINARY_SMOKE=1`. **Deferred to Week 3 with tracking**: D.7 — `event_triggers` consumer (full evaluator), required for `pr_opened` → `wf-review` auto-fire; multi-tier dispatch (`compute_tier` column reserved in DB, future ADR adds back when a non-`standard` tier arrives); real GitHub mode (`gh pr create` etc.), explicitly Phase 4 work per the parent plan, worker raises on `REPO_MODE=github` until then; auto-restart for the consumer task if it dies (current behavior is operator-visible via `/health/ready` 503; future ADR adds supervisor + auto-restart). Week 2 of the parent plan satisfied every adversarial-review finding except those explicitly deferred above.

## Post-mortem

### What worked

- **Parallel agents per cluster, file-isolated partitioning.** Phase 1 fanned out four agents across four mostly-disjoint partitions (worker package, API package, CDK + infra, workspace + hooks). The integration pass produced zero merge conflicts. Phase 4 reused the same pattern — three agents (worker tests, CDK DLQ + behavioral test, plan-doc rewrite) on disjoint surfaces, again clean. File-level partitioning is cheap to plan up-front and pays for itself by removing the human conflict-resolution step entirely on the happy path.
- **The open-questions packet.** Eighteen open architectural questions ratified in a single user-blessed sitting (decisions #1–#18 at the top of this plan). Each decision unblocks 1–3 work items downstream. Compared to the alternative — re-litigating each decision when its work item starts — the up-front packet saved an estimated 12+ chat round-trips, and produced a written artifact that all four agents could refer to without ambiguity.
- **Phasing aligned with dependency edges, not arbitrary day counts.** The four phases (foundation → build on foundation → compose → capstone) drop out of the dependency graph between work items. A.2 lands first because A.1/A.3/A.4 import the shared schema; B.8 follows A.4 because it's the consumer of `AuthorStepOutput`; D.2 follows D.1 because it gates on rows D.1 persists. No agent waited on an artifact that hadn't shipped yet.
- **Honesty wins shipped early.** Phase 1's `--allow-empty` removal, branch-format fix, and `gh` mode raise are tiny diffs but unblock the architectural conversation downstream. Putting them in Phase 1 rather than mixing them in with Phase 2's Pydantic boundary work kept the harder work focused.

### What we'd do differently

- **Multi-agent integration testing of file-level conflicts could be smoother.** Two agents touched `workers/agent/treadmill_agent/runner.py` in two phases (Phase 1 added `EXIT_AFTER_STEP` config + dropped `compute_tier`; Phase 2 reordered the `_handle_step` flow + added the no-staged-changes failure path). The merge held because the changes were in non-overlapping function bodies, but the *concept* of two agents editing the same file across two phases is fragile. A worktree-per-cluster approach (one Git worktree per agent, hand-merged at phase boundaries) would have been more rigorous; we got lucky.
- **Test-totals accounting at phase boundaries needed a fixture.** Each agent reported its own delta; the integrator had to aggregate by hand at the end. A `pytest --collect-only --quiet` baseline captured at phase boundary, plus a script that compares to the new baseline, would make the running-log numbers automatic. Captured for a future small tool.
- **D.7 framing could have been clearer up front.** The `event_triggers` consumer was the one item where the reviewer recommended "ship partial." We rejected that framing (the collapse-then-restore pattern the user has previously rejected — see `docs/learnings/2026-05-07-collapse-then-restore.md`) but documented the deferral inside this plan's "Explicitly deferred to Week 3" rather than as its own ADR or tracked-issue artifact. Week 3 should open with the `event_triggers` ADR before the consumer's prompt design.

### What the adversarial review caught that we wouldn't have on our own

The 2026-05-11 manual adversarial reviewer surfaced four findings that the orchestrator alone almost certainly would not have caught — these were not "code that looked wrong" but "code that *passed all its tests* but violated an architectural contract":

- **The dry-run smoke being passed off as Phase 2 success criterion 4 satisfaction.** The Week-2-closed running-log entry said "End-to-end smoke verified live." Technically true: a step ran end-to-end. Architecturally false: the worker wrote a `.treadmill/<step_id>.md` marker file instead of authoring code. Phase 2 success criterion 4 says "an authoring worker picks up a task, branches, **authors the change**." A marker file is not an author. We had built and shipped a green-test pipeline that wasn't *doing* the thing the plan required. Only an outside reviewer reading the criterion against the closure note caught this.
- **The worker→consumer Pydantic-boundary asymmetry.** ADR-0011 says "Pydantic at every boundary." The API publish path validated via typed event payload classes; the worker built raw dicts. The asymmetry was invisible to the test suite because each side's tests asserted only its own behavior. A boundary contract that's honored on only one side is the same as no contract — but the local view from either side looks fine.
- **The `task_prs` writer never wired.** The webhook→trigger chain depends on `task_prs` being populated when a worker publishes `step.completed` with a `pr_number`. The worker emitted the field; nothing read it. Every component test passed; the integration never fired. This is the canonical "two halves of a chain shipped separately, the bridge never built" pattern — and the only way to catch it is a reviewer reading the contract.
- **The branch format quietly diverging from ADR-0010.** ADR-0010 §"Branch conventions" specifies `task/<short-id>-<slugified-title>`. The worker shipped `task/<short>/<step_name>` and the unit test asserted the wrong format. Both ends agreed; the ADR did not. A test that pins the wrong shape is worse than no test, because it *prevents* the right shape from arriving without explicit override.

### The meta-lesson

At the start of an architectural shift, every phase boundary triggers a full review-against-contracts pass. The contracts are the ADRs and the active plan-doc. The reviewer's question is not "did the code work" but "did the code honor the contract." This discipline was captured as `docs/learnings/2026-05-11-review-driven-phase-closure.md` after the user's correction, and this closure plan was its first test case. It paid: the closure absorbed ~30 findings, four of which were load-bearing in the sense described above. The cost of letting any one of those four compound across Weeks 3 and 4 — every downstream tool taking a dependency on the wrong shape, then needing retrofit — would have been multiples of this closure's price.

When the planned `/ultrareview` analogue lands inside Treadmill itself, the dispatch of the reviewer moves from manual to substrate. Until then, the orchestrator's responsibility is to *invoke the discipline at every phase boundary*, even when the local view says the work is done.
