# Plan: ADR-0084 Coordinator-Led Team Execution Model — Implementation

- **Status:** drafting
- **Date:** 2026-06-08
- **Related ADRs:** ADR-0084, ADR-0011, ADR-0018, ADR-0025, ADR-0029, ADR-0031, ADR-0067, ADR-0068, ADR-0073
- **Research inputs:** operator-team research sprint 2026-06-08 (alan/bert/carla/donna)

## Goal

Implement ADR-0084's coordinator-led team execution model end-to-end on the personal deployment. Prove the model works for at least one plan executed by a coordinator + 2 workers before expanding to multi-team operation. Retire the autoscaler and hard attempt caps once the coordinator can replace their functions.

## Success criteria

1. A coordinator session (`coordinator-<repo>`) can be launched via systemd and receives all plan-scoped SQS events (not just its own `created_by` work).
2. The coordinator can brief a named worker via cc-relay, track task state in `task_board`, and route a CI failure signal back to the author worker.
3. At plan close, the coordinator writes a per-repo memory file.
4. The autoscaler (`tools/local-adapter/treadmill_local/autoscaler.py`) is no longer running and its function (worker provisioning) is handled by the coordinator.
5. Hard attempt caps in `coordination/triggers.py` fall back to coordinator-decision flow; caps-as-backstop remain in code but are not the primary enforcement path.

## Key findings from research (decisions encoded here)

### Substrate (Bert)
- Coordinator is a standard named session launched via the same systemd template. No new wrapper layer. Workdir convention: `~/.treadmill/teams/<repo-slug>/`.
- **One real code change**: `treadmill-events.ts` SQS filter must widen from `created_by === LABEL` to include `plan_id`-based subscription for coordinator labels. Mechanism: `TREADMILL_ROLE=coordinator` env var + `TREADMILL_COORDINATOR_PLANS=<id>,<id>` set at session launch time (coordinator subscribes on plan start).
- Coordinator-channel mode (operator-instance vs worker roles): single inbox + `[ROLE: coordinator-escalation]` or `[ROLE: worker-brief]` header convention. No server change needed in v1.
- Memory ceiling: ~400–500 MB per session. 16 GB machine supports ~25 sessions (5 teams × 5 sessions).

### Communication (Carla)
- **Immediate fix shipped**: MAX_LEN 4096 → 32768, filename collision fix (`time_ns + token_hex(2)`). Both in cc-relay.py.
- Delivery is NOT guaranteed: read-then-unlink before `mcp.notification` means a channel-server crash loses the message. Acceptable for v1; delivery confirmation (ack protocol) is a v2 investment.
- `ACTION REQUEST` header is pure convention — zero server-side effect. Wake-up behavior is CC-side.
- ADR §3 "some channel server configurations" wording is misleading; the server is convention-blind. The ADR will be amended before implementation begins.
- Coordinator-channel mode: **subfolder convention** (`relay/coord/` vs `relay/worker/`) with ~10 LOC server change. Defers two-watcher complexity (Carla option c). cc-relay.py gains `--subfolder coord|worker` flag.
- cc-relay gaps to address in v1: batch send (`--to-many`), bump MAX_LEN (done), structured metadata (`--meta`). Delivery confirmation + ack protocol deferred to v2.

### State (Donna)
- `tasks` table has no status column; `task_board` is a new separate table (append-only invariant on `tasks` must not be broken).
- `task_board` migration: see SQL schema in Donna's research (includes `updated_by TEXT` for coordinator label).
- Attempt caps live in `coordination/triggers.py` as five named constants + `_is_capped()`. Retirement path: coordinator-decision check before `_is_capped()` fallback; caps stay in code as backstop.
- Autoscaler (`tools/local-adapter/treadmill_local/autoscaler.py`, 609 lines) retires wholesale.
- `consumer.py` (2366 lines) mixes event projection and plan routing. **Split required**:
  - Event projector: stays centralized, single-writer invariant preserved (ADR-0011).
  - Plan routing: moves to per-plan coordinator via Option A — projector emits internal `step.projection_completed` event; coordinator subscribes and decides downstream dispatch.
  - `auto_merge_loop` (asyncio task in consumer `__init__`): moves to coordinator. It is a per-plan auto-merge decision, not a substrate concern.

## Constraints / scope

### In scope
- `treadmill-events.ts` SQS filter widening for coordinator labels (TREADMILL_ROLE + TREADMILL_COORDINATOR_PLANS)
- `relay/coord/` subfolder convention + cc-relay `--subfolder` flag + 10 LOC server change
- cc-relay.py: `--to-many` batch flag; `--meta` structured metadata
- `task_board` Alembic migration
- Coordinator session launch convention (workdir, env vars, systemd naming)
- Coordinator briefing prompt (v1 — single repo, coordinator + 2-4 workers)
- `consumer.py` projection/routing split + `step.projection_completed` internal event
- `auto_merge_loop` extraction to coordinator scope
- `_is_capped()` callsites: add coordinator-check-first path
- Autoscaler retirement

### Out of scope (v1)
- Multi-team (multiple concurrent coordinators on different repos) — run one team first
- Delivery confirmation / ack protocol — best-effort delivery acceptable in v1
- Per-repo memory compaction — append-only markdown file at plan close; no compaction
- Concurrent coordinators on same repo (ADR-0084 alternative I) — explicitly deferred
- Coordinator DSPy prompt optimizer — deferred to post-proof
- cc-relay TTL/staleness, idempotency keys, connectivity check

### Budget
Operator-team direct implementation (alan/bert/carla/donna). No autoscaler spin-up budget during this track — keep at 0 workers until the coordinator is ready to replace it.

## Sequence of work

### Phase 0 — Transport fixes (DONE 2026-06-08)
- [x] cc-relay.py: MAX_LEN 32768, nanosecond+random filename collision fix
- [ ] ADR-0084 §3 wake-up wording amendment (ACTION REQUEST is convention-only, not server-enforced)

### Phase 1 — Coordinator substrate (2–3 days, can be parallelized)

**Task 1A** — cc-relay.py coordinator features
- `--to-many "bert,carla,donna"` flag for broadcast
- `--subfolder coord|worker` flag (writes to `relay/coord/*.md` or `relay/worker/*.md`)
- `--meta key=val` structured metadata (appended as frontmatter to relay file)
- Tests: update cc-relay tests for new flags; confirm no regression on existing behavior

**Task 1B** — treadmill-events.ts: coordinator SQS subscription
- Read `TREADMILL_ROLE` env var; if `coordinator`, widen `isMine()` to also match `plan_id` against `TREADMILL_COORDINATOR_PLANS` (comma-separated list)
- `startRelayWatcher` update: if `TREADMILL_RELAY_PATHS` includes `relay/coord/`, watch that subdir in addition to `relay/`
- `mcp.notification` carries `meta.subfolder` so receiving session can route attention
- Tests: inject relay files to both paths; confirm both surface as channel notifications

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
- Add `GET /api/v1/task_board/{plan_id}` read endpoint (coordinator queries on startup reconciliation)
- Add `PATCH /api/v1/task_board/{task_id}` write endpoint (coordinator updates status + assignee)
- Tests: upsert, status vocab validation, FK cascade

### Phase 2 — Consumer split (2–3 days)

**Task 2A** — Extract event projector
- `coordination/consumer.py` → split: `EventProjector` handles projection-only writes (workflow_run_steps.status, the single-writer path per ADR-0011); `PlanRouter` handles cross-step dispatch, feedback trigger, conflict resolution
- `EventProjector` emits internal `step.projection_completed` event after each DB write
- `auto_merge_loop` extracted to a new `AutoMergeCoordinator` class; wired to coordinator instead of consumer `__init__`
- Existing consumer tests must stay green; refactor does not change observable behavior in this phase

**Task 2B** — Cap retirement path
- `_is_capped()` callsites: before invoking cap check, query `task_board` for a coordinator-set `blocked_operator` status; if set, skip dispatch (coordinator has already escalated)
- Retire constants as actual enforcement: keep in code with a `# backstop — coordinator judgment is the primary gate` comment
- Tests: confirm tasks in `blocked_operator` don't dispatch despite being under the cap count

### Phase 3 — Coordinator session + briefing protocol (3–4 days)

**Task 3A** — Coordinator launch + workdir convention
- Systemd naming convention: `treadmill-channel@coordinator-<repo-slug>.service`
- `launch-session.sh`: if `TREADMILL_ROLE=coordinator` is set, skip dispatch-reminder, set workdir to `~/.treadmill/teams/<repo-slug>/`
- `treadmill-events.ts`: TREADMILL_COORDINATOR_PLANS populated via new CLI arg or env file at `~/.treadmill/teams/<repo-slug>/coordinator.env`

**Task 3B** — Coordinator briefing prompt v1
- `tools/coordinator/coordinator_prompt.md`: v1 coordinator system prompt covering task brief format, signal routing table, task_board API calls, per-repo memory read/write, escalation chain
- `tools/coordinator/brief_worker.py`: helper that templates the task brief (scope, active peers, pitfalls from per-repo memory, ownership claims format)
- Integration test: coordinator reads a plan from DB, generates briefs for 2 workers, those briefs contain all required fields

### Phase 4 — Autoscaler retirement (1 day)

**Task 4** — Remove autoscaler from treadmill-local
- Stop and disable `autoscaler` in `treadmill-local up` and related commands
- Delete `tools/local-adapter/treadmill_local/autoscaler.py` (609 lines)
- Remove autoscaler-related config from `personal.yaml` (`autoscaler:` block)
- Confirm `treadmill-local up` still works without autoscaler
- Update AGENT.md and any docs that reference autoscaler

### Phase 5 — First end-to-end proof (2 days)

Run a real plan on `medicoder` (RAMJAC) using one coordinator + 2 workers. Measure: coordinator token budget (session total), worker token budget per task, total vs prior per-task baseline. Document findings as a learning. If coordinator overhead > rework savings, the model needs adjustment per ADR-0084 context.

## Risks / unknowns

- **consumer.py split complexity**: at 2366 lines with entangled asyncio tasks, the projection/routing split may reveal more coupling than expected. Budget an extra day if 2A drags.
- **SQS filter TREADMILL_COORDINATOR_PLANS population**: the coordinator needs to know its plan IDs at launch. A coordinator session may be launched before a plan is assigned to it. Solution: coordinator.env is written at plan-start by the API; coordinator reloads it; or the coordinator polls the API at startup. Pick one in Task 3A.
- **coordinator.env reload mechanism**: if the coordinator needs to subscribe to multiple plans over its lifetime, the `treadmill-events.ts` needs a way to add plan IDs at runtime without restarting. May require a small coordination API or a watched file. Defer to implementation if startup-only subscription is sufficient for v1.
- **Coordinator context length vs. long plans**: a coordinator watching a 20-task plan accumulates routing context quickly. The §3 re-brief pattern (summarize + re-brief) is the mitigation; the coordinator briefing prompt must include explicit self-compaction guidance.

## Decisions captured during execution

_Populated as work proceeds._

## Post-mortem

_Filled in when complete or abandoned._
