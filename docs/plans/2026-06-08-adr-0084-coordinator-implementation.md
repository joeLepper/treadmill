# Plan: ADR-0084 Coordinator-Led Team Execution Model — Implementation

- **Status:** drafting
- **Date:** 2026-06-08
- **Related ADRs:** ADR-0084, ADR-0011, ADR-0018, ADR-0025, ADR-0029, ADR-0031, ADR-0067, ADR-0068, ADR-0073
- **Research inputs:** operator-team research sprint 2026-06-08 (alan/bert/carla/donna)
- **Amendments:** v2 — unanimous-vote revisions from bert/carla/donna (2026-06-08)

## Goal

Implement ADR-0084's coordinator-led team execution model end-to-end on the personal deployment. Prove the model works for at least one plan executed by a coordinator + 2 workers before any irreversible infrastructure is removed. Retire the autoscaler and hard attempt caps only after the coordinator demonstrates it can replace their functions.

## Success criteria

1. A coordinator session (`coordinator-<repo>`) can be launched via systemd and receives all plan-scoped SQS events.
2. The coordinator can brief a named worker via cc-relay, track task state in `task_board`, and route a CI failure signal back to the author worker.
3. At plan close, the coordinator writes a per-repo memory file.
4. Phase 5 proof: coordinator drives ≥10 tasks on RAMJAC with amend rate ≤30% (calibration threshold before long-run 20% target). Coordinator handoff produces a document that allows a new coordinator session to resume routing correctly.
5. Only after Phase 5 is green: autoscaler is no longer running; caps stay as hard-stop backstop.

## Key findings from research

### Substrate (Bert)
- Coordinator is a standard named session via the same systemd template. No new wrapper layer. Workdir: `~/.treadmill/teams/<repo-slug>/`.
- **One real code change**: `treadmill-events.ts` SQS filter widens from `created_by === LABEL` to include `plan_id`-based subscription. Client-side AND server-side: the WS `?created_by=` subscription at line 204 must also be addressed (not just `isMine()` at line 171). Mechanism: `TREADMILL_ROLE=coordinator` + `TREADMILL_COORDINATOR_PLANS=<id>,<id>` env vars.
- Coordinator-channel mode: single inbox + `[ROLE: ...]` header convention. No server change needed in v1.
- Memory ceiling: ~400–500 MB per session. 16 GB machine supports ~25 sessions.

### Communication (Carla)
- **Phase 0 fixes shipped** (97086a3): MAX_LEN 4096→32768; nanosecond+random filename collision fix.
- Delivery is NOT guaranteed; best-effort acceptable in v1.
- `ACTION REQUEST` header is pure convention — zero server-side effect. ADR §3 amended (97086a3).
- Coordinator-channel mode: subfolder convention (`relay/coord/`) with ~10 LOC server change.
- cc-relay gaps to address in v1: `--to-many` batch flag, `--meta` structured metadata.

### State (Donna)
- `task_board` is a new separate table (append-only invariant on `tasks` must not be broken).
- Attempt caps live in `services/api/treadmill_api/coordination/triggers.py` — five constants + `_is_capped()`. Coordinator-check is an EARLIER-STOP OVERLAY; caps remain as hard-stop backstop. Constants are not relaxed.
- Autoscaler (`tools/local-adapter/treadmill_local/autoscaler.py`, 609 lines) retires wholesale — but only after Phase 5 proof.
- `services/api/treadmill_api/coordination/consumer.py` (2366 lines) mixes event projection and plan routing. Split required: projector emits internal `step.projection_completed` event; coordinator subscribes (Option A, per Donna). `auto_merge_loop` moves to coordinator — but NOT until Phase 3 (coordinator exists to host it).

## Constraints / scope

### In scope
- `treadmill-events.ts`: coordinator SQS + WS subscription widening
- `relay/coord/` subfolder convention + cc-relay `--subfolder` flag + server change
- cc-relay.py: `--to-many` batch flag; `--meta` structured metadata
- `task_board` Alembic migration + read/write API endpoints
- Coordinator session launch convention (workdir, env vars, systemd naming)
- Coordinator briefing prompt v1 + handoff doc generator
- `consumer.py` projection/routing split + `step.projection_completed` internal event
- `auto_merge_loop` extraction (in Phase 3, after coordinator exists)
- `_is_capped()`: add coordinator-check-first path (caps remain as hard-stop backstop)
- Autoscaler: disable in Phase 4a, delete in Phase 4b (after Phase 5 proof)

### Out of scope (v1)
- Multi-team concurrent operation — one team first
- Delivery confirmation / ack protocol — best-effort acceptable
- Per-repo memory compaction — append-only markdown, no compaction
- Concurrent coordinators on same repo (ADR-0084 alternative I)
- cc-relay TTL/staleness, idempotency keys, connectivity check

### Budget
- Target window: 10 working days for Phases 0–5. Abort and reassess at day 14.
- Phase 5 coordinator token budget: cap coordinator session at 200K tokens per plan (escalate to operator if approaching limit); measure actual spend as the baseline.
- No autoscaler spin-up during Phase 1–4a. If autoscaler=0 becomes a problem before the coordinator is ready, pause Phase 1 and discuss with Joe — don't quietly re-enable.

## Phase 0 — Transport fixes (COMPLETE)

- [x] cc-relay.py: MAX_LEN 32768, nanosecond+random filename collision fix (97086a3)
- [x] ADR-0084 §3 wake-up wording amendment (ACTION REQUEST is convention-only) (97086a3)

Phase 0 must be complete before Phase 1 begins. It is complete.

## Phase 1 — Coordinator substrate (2–3 days, parallelizable)

**Task 1A** — cc-relay.py coordinator features
- `--to-many "bert,carla,donna"` flag for broadcast (one file drop per target)
- `--subfolder coord|worker` flag (writes to `relay/coord/*.md` or `relay/worker/*.md`)
- `--meta key=val` structured metadata (appended as YAML frontmatter to relay file)
- Tests: new flags, no regression on existing behavior

**Task 1B** — treadmill-events.ts: coordinator SQS + WS subscription
- Read `TREADMILL_ROLE` env var; if `coordinator`, widen `isMine()` (line 171) to also match `plan_id` against `TREADMILL_COORDINATOR_PLANS`
- Server-side WS subscription (`?created_by=` at line 204): coordinator must use a different subscription param (e.g. `?plan_ids=`) or drop the `created_by` filter entirely for the coordinator path. Pick one and document — without this, `isMine()` sees an already-filtered feed and the coordinator misses events.
- `startRelayWatcher`: if subfolder `relay/coord/` exists, watch it alongside `relay/`; `mcp.notification` carries `meta.subfolder` so receiving session can route attention
- Tests: relay files to both paths surface as channel notifications; coordinator receives plan-scoped events from other workers' tasks

**Task 1C** — task_board Alembic migration
```sql
CREATE TABLE task_board (
    task_id UUID PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    plan_id UUID NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    assignee TEXT,
    status TEXT NOT NULL,
    branch TEXT,
    pr_number INTEGER,
    notes TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by TEXT
);
CREATE INDEX ix_task_board_plan_id ON task_board(plan_id);
CREATE INDEX ix_task_board_assignee ON task_board(assignee);
CREATE INDEX ix_task_board_status ON task_board(status);
```
- `GET /api/v1/task_board/{plan_id}` — coordinator reads on startup reconciliation
- `PATCH /api/v1/task_board/{task_id}` — coordinator writes status + assignee
- Tests: upsert, status vocab validation, FK cascade, reconciliation against `task_status` view

## Phase 2 — Consumer split (2–3 days) — IN PROGRESS

**Task 2A** — Extract event projector from consumer.py ✓ FULLY MERGED — Phase 1 (a8bf1f6f / PR #256) + Phase 2 (e5ff9d75 / PR #258). consumer.py 2366→623 lines; PlanRouter 1694 lines; trace-replay harness green (78.9% fixture coverage; capture-pipeline fix follow-up tracked)
- Split `services/api/treadmill_api/coordination/consumer.py` (2366 lines): `EventProjector` handles projection-only DB writes (single-writer path per ADR-0011); `PlanRouter` handles cross-step dispatch, feedback trigger, conflict resolution
- `EventProjector` emits internal `step.projection_completed` event after each DB write
- **Behavior-equivalence gate** (Bert): before merging, capture a 1-hour event trace against a live RAMJAC plan; replay through both old + new code paths; assert identical DB writes + emitted events. Trace-replay-equivalence is the merge gate, not test-suite-green alone.
- `auto_merge_loop` stays wired to `consumer.__init__` as a transitional shim through this phase. It moves to the coordinator in Phase 3.
- Existing consumer tests must stay green; refactor does not change observable behavior

**Task 2B** — Cap retirement: coordinator-check-first path — Bert rebasing + marking #254 ready
- At each `_is_capped()` callsite in `services/api/treadmill_api/coordination/triggers.py`: before invoking the existing cap logic, check `task_board` for `blocked_operator` status on this task's plan
- **Coordinator-liveness guard** (Bert): if `task_board.updated_at` for any task in this plan is older than N minutes (configurable, default 15), the coordinator is absent — skip the coordinator-check and apply cap behavior immediately. Coordinator absence = caps actively load-bearing.
- **Caps remain hard-stop backstop** (Carla): constants are not relaxed. Coordinator-check is an earlier-stop overlay. A coordinator-blocked task stops before the cap fires; an uncoordinated task still hits the cap.
- Task_board query note: this is on a moderately hot path. Cache `blocked_operator` status per plan_id in memory (TTL 30s); invalidate on `PATCH /task_board`. Specify caching strategy in the PR.
- Tests: blocked_operator stops dispatch before cap; liveness-check absent coordinator falls back to cap; cap fires at limit when neither condition applies

## Phase 3 — Coordinator session + briefing protocol (3–4 days)

**Task 3A** — Coordinator launch + workdir convention ✓ MERGED (e77ec38 / PR #252)
- Systemd naming: `treadmill-channel@coordinator-<repo-slug>.service`
- `launch-session.sh`: if `TREADMILL_ROLE=coordinator`, skip dispatch-reminder, set workdir `~/.treadmill/teams/<repo-slug>/`; `set -a` sources `coordinator.env` so bare KEY=value lines export across `exec`
- `coordinator.env.template` + README in `tools/coordinator/`
- `coordinator.env` file written by API at plan-start with `TREADMILL_COORDINATOR_PLANS=<id>,...`; coordinator reads on startup (reload path deferred to Task 3A follow-up per v1 scope)

**Task 3B** — Coordinator briefing prompt v1 ✓ MERGED (cb31afcb / PR #253)
- `tools/coordinator/coordinator_prompt.md`: system prompt covering task brief format, signal routing table, task_board API calls, per-repo memory read/write, escalation chain, self-compaction guidance (re-brief at context limit)
- `tools/coordinator/brief_worker.py`: helper templating task brief (scope, active peers, pitfalls from per-repo memory, ownership claims format)
- Quality gate for cap retirement: coordinator has successfully brokered ≥10 tasks (real run) AND amend rate on those 10 ≤30%. If >30%, briefing prompt iterates before Phase 4a.

**Task 3C** — auto_merge_loop migration to coordinator ✓ MERGED (e624934 / PR #261)
- `AutoMergeLoop` extracted to `coordination/auto_merge_loop.py` (117 lines); consumer constructs + lifecycles it; consumer shrank 623→597 lines. Full migration to coordinator session is Phase 6.
- Follow-up (PR #263 / fa0335f7): synthetic trace fixture replaces malformed RAMJAC capture; 56-event deterministic generator, schema v2, _MIN_REPLAYED_EVENTS floor removed.
- Follow-up (PR #265 / 35382159): `__getattr__/__setattr__` back-compat shim removed from CoordinationConsumer; consumer.py 597→542 lines; PlanRouter surface permanent.

**Task 3D** — Coordinator handoff doc generator ✓ MERGED (a79985d8 / PR #255)
- API endpoint or coordinator-side script to snapshot: current task board, per-worker lane summary, unresolved signals, operator-instance designation
- Coordinator prompt includes: "at N-50K tokens remaining, generate handoff doc and relay to incoming coordinator"
- Handoff-receive prompt: incoming coordinator reads handoff + runs §6 restart reconciliation procedure
- Phase 5 sub-criterion: coordinator approaches context limit → produces handoff doc → coordinator-2 resumes routing correctly

## Phase 4a — Disable autoscaler (1 hour) ✓ PREP MERGED (10b4a319 / PR #257)

- `autoscaler.enabled` flag added to local-adapter config (default true; false suppresses spawn)
- `--no-autoscaler` CLI flag AND `enabled=false` config both gate the spawn; differentiated log messages
- personal.yaml unchanged — flag flip (`enabled: false`) is the Phase-5-eve operator step

**Reversal**: flip flag back to true. 30 seconds.

## Phase 5 — End-to-end proof on RAMJAC (2 days) — IN PROGRESS

**Testbed plan**: `b3a8bd29-38d9-448c-85f9-046eb493c855` — GCP observability stack +
dashboards-as-code (7 tasks). Submitted 2026-06-09T06:37Z by coordinator-medicoder.
coordinator.env updated to include plan ID. Workers briefed via cc-relay.

**Task routing** (2026-06-09):
- Bert: otel-collector-deploy (78af1379), dispatcher-coverage (449d380f)
- Bert: otel-collector-deploy (78af1379) blocked on operator prereqs, dispatcher-coverage (449d380f)
- Donna: datadog-dashboards-as-code (9959c49c) ✓ MERGED PR #1231, dedup-purge-cloud-run-job (9bbda236) ✓ MERGED PR #1233 + lib PR #26, chain-dep-bump-0.1.8 (07ad049d) in flight
- Carla: consumer-subscription-validation (7877139d) ✓ MERGED PR #25 (medicoder-events), dashboard-generator-gcp (9c586a77) in flight, planrouter-fixture-expansion (4e3cffc2)
- Alan: cloud-trace-verify (82179115) — blocked on otel-collector-deploy

**Brokered count so far**: 3 merged
- 9959c49c / PR #1231 (datadog-dashboards-as-code) 2026-06-09T06:39Z
- 7877139d / PR #25 medicoder-events (consumer-subscription-validation) 2026-06-09T06:46Z
- 9bbda236 / PR #1233 + medicoder-events PR #26 (dedup-purge) 2026-06-09T06:49Z

Run a real plan on RAMJAC using coordinator-medicoder + 2 workers. Verify:
- Coordinator provisions workers (not autoscaler)
- CI failure signal routes correctly to author worker
- Coordinator writes per-repo memory at plan close
- Coordinator handoff works (Task 3D sub-criterion)
- Token budget: coordinator session + worker sessions combined vs prior per-task baseline
- Quality gate: ≥10 tasks brokered, amend rate ≤30%

**Abort criteria + rollback order** (Carla + Donna): if Phase 5 reveals a coordinator gap that blocks the proof:
1. Re-enable autoscaler (revert Phase 4a config flip — 30 seconds)
2. Demote coordinator-veto path to off in triggers.py (one-line commit)
3. If auto_merge_loop migration (3C) is already merged, revert that PR and re-wire to consumer.__init__

Each step is a small commit; total revert cost < 1 day.

## Phase 4b — Delete autoscaler (1 day, after Phase 5 is green)

- Delete `tools/local-adapter/treadmill_local/autoscaler.py` (609 lines)
- Remove `autoscaler:` block from `personal.yaml`
- Remove autoscaler from `treadmill-local up` entirely
- Update AGENT.md and all docs that reference autoscaler
- **Rollback recipe** (Bert): Phase 4b PR is on a dedicated branch, not squash-merged. `git revert` of Phase 4b restores autoscaler.py and the personal.yaml block in one step. CHANGELOG entry in the PR body states the exact personal.yaml block removed.

## Risks / unknowns

- **consumer.py split complexity**: at 2366 lines with entangled asyncio tasks, the trace-replay-equivalence gate (Task 2A) is the load-bearing safeguard. If the trace reveals ordering quirks that aren't covered by existing tests, scope a separate "consumer hardening" task before the split merges.
- **TREADMILL_COORDINATOR_PLANS population + reload**: coordinator may be launched before plan assignment. `coordinator.env` written by API at plan-start is the mechanism; if the coordinator needs to subscribe to multiple plans over its lifetime, the reload path (file watch or API poll) needs to be designed in Task 3A before Task 1B ships.
- **Coordinator context length**: Phase 5's coordinator session is capped at 200K tokens. If it hits the limit before 10 tasks complete, the handoff protocol (Task 3D) is the recovery path — not option to exceed the cap.
- **Phase 5 quality gate dependency**: cap retirement (Task 2B demotion) depends on ≥10 tasks at ≤30% amend rate. If the first Phase 5 run hits 10 tasks but at 45% amend rate, the coordinator iterates before Phase 4b ships. This is expected; the ramp-up allowance in ADR-0084 §9 exists for exactly this.

## Decisions captured during execution

_Populated as work proceeds._

## Post-mortem

_Filled in when complete or abandoned._
