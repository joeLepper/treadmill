# Plan: Treadmill operator dashboard v1 (Overview + Task Detail)

- **Status:** drafting
- **Date:** 2026-05-26
- **Related ADRs:** ADR-0020 (observability — Grafana for metrics, this dashboard is for pipeline state, distinct surface), ADR-0035 (scheduler primitive — health-bot escalations surface here)

## Goal

Replace the "psql + `docker logs` + curl + `gh`" context-switch with a small operator dashboard that answers two questions an operator asks dozens of times a day:

1. **"What's happening right now?"** — every non-terminal task at a glance, recent events, worker fleet, any health-bot escalations.
2. **"Why is *this* task here?"** — drill into one task: its workflow runs, steps with outputs/errors, the PR it produced and its mergeability state, what it cost, and the affordances to intervene (cancel / override / retry / open-PR).

## Success criteria

- A single SPA at `services/dashboard/` reachable from the operator's browser with the two pages above, pulling live data from the existing Treadmill API.
- Overview refreshes within 5s of a state change (poll or WebSocket).
- Cancelling a task from the UI inserts the canonical cancellation event and the row's `derived_status` flips to `cancelled` on the next refresh.
- Visual identity descends from the bunkhouse dashboard (`bunkhouse/services/dashboard/`) — same DataTable, FilterBar, status-badge vocabulary, sidebar layout — so a bunkhouse operator recognizes the shape immediately. Per `feedback_bunkhouse_precedent_shapes`.
- Per-account spend strip on the overview (ADR-0055 made this first-class). Granular spend page is **out of scope** for v1.
- Builds and runs as its own container in `treadmill-local up`, served on a distinct host port; nginx-served Vite build per bunkhouse precedent.

## Constraints / scope

### In scope
- Two pages: **Overview** (`/`) + **Task Detail** (`/tasks/:id`).
- Lift the bunkhouse dashboard scaffolding wholesale: Vite + React 19 + TypeScript + Tailwind + TanStack Query + the `WebSocketContext` subscription pattern from `bunkhouse/services/dashboard/src/contexts/WebSocketContext.tsx`.
- New API: a small set of aggregation/list endpoints for the dashboard's needs (most data is already projection-shaped in the `task_status` / `task_mergeability` views).
- New: `/ws/events` WebSocket on the API service emitting plan/task/run/step lifecycle events.
- Action affordances backed by existing API endpoints + event inserts.

### Out of scope (defer)
- **Auth.** v1 is **single-operator local** (no login). Bunkhouse uses Google OAuth; we punt until the dashboard needs to be reachable beyond `localhost`.
- **Other routes** — plans index, repos config, schedules, roles/skills/hooks management. Lift bunkhouse's pages when the need is felt, not pre-emptively.
- **Per-account spend page** (history, charts) — overview gets the strip, full page comes later.
- **Submit-work flow** — `/author` skill + `POST /api/v1/plans` already covers this cleanly.

### Budget
Two-to-three focused dev days end-to-end, split roughly: Claude Design visual exploration (operator), scaffold lift + data wiring (me), action affordances + deploy (me).

## Sequence of work

1. **Data spec (this doc, below)** — pasted into Claude Design when the operator starts the visual exploration, so the canvas has correct field shapes from the start instead of vibes data.
2. **Claude Design exploration** — operator points Claude Design at `bunkhouse/services/dashboard/` (persistent-design-system feature) so Treadmill's design system descends from bunkhouse's. Iterate the two pages with realistic-looking placeholder data shaped per §"Data shapes" below. Output: a "Send to Claude Code" handoff bundle.
3. **Scaffold lift.** Create `services/dashboard/` mirroring `bunkhouse/services/dashboard/`: Vite + React 19 + TS + Tailwind + TanStack Query + `WebSocketContext` + DataTable + FilterBar + PageLayout. Pull in the Claude Design bundle's visual layer + design tokens; rewire chrome to Treadmill names.
4. **Data wiring.** Add the aggregation endpoints in `services/api/treadmill_api/routers/dashboard.py` (Overview and Task Detail need a small handful of joined queries). Wire TanStack Query for polling-first (3-5s); SSE/WS can come if polling stings.
5. **WebSocket events.** Add `/ws/events` to the API; emit task/run/step lifecycle events as `EventBus` publishes already happen. Dashboard subscribes via lifted `WebSocketContext`.
6. **Action affordances.** Wire Cancel (event insert), Open PR (deeplink to GitHub), Retry-last-step (existing dispatcher path), Override-review (if `validate.override` exists per ADR-0042 — confirm before wiring).
7. **Container + local-adapter integration.** Multi-stage Dockerfile (Node build → nginx serve); `tools/local-adapter/treadmill_local/runtime.py` adds a `treadmill-dashboard` service spec with a distinct host port. `treadmill-local up` brings it up alongside the API.
8. **Smoke + first cut.** Open in browser. Walk through both pages with the live deployment. File polish items as follow-ups; **don't expand scope inside this PR**.

## Data shapes — paste this section into Claude Design

> The dashboard reads from these projections. Use these field shapes for placeholder data in the canvas. Real Postgres views; the API surfaces them via the aggregation endpoints below.

### Overview page — what the operator sees

**Header strip:**
- Total non-terminal tasks (count by stage).
- Worker fleet — running worker count, autoscaler "alive since" heartbeat timestamp.
- Scheduler last-tick timestamp.
- Per-account spend last 24h, by account name (`personal`, `zephyr`, …) — token total + USD estimate.
- Escalations badge (count of tasks carrying a `task.escalated_to_operator` event).

**Main table — non-terminal tasks:**
Source: `task_status` view, filtered `WHERE derived_status NOT IN ('done','cancelled','pr_merged')`, joined with `task_prs` + `task_mergeability` for PR columns.

| Column | Type | Source |
|---|---|---|
| `repo` | string `owner/name` | `task_status.repo` |
| `title` | string | `task_status.title` |
| `plan` | uuid (linked) | `task_status.plan_id` |
| `stage` | enum | `task_status.derived_status` — values: `registered`, `blocked`, `wf-quick: executing`, `wf-review: executing`, `wf-ci-fix: executing`, `wf-feedback: executing`, `wf-validate: executing`, `wf-conflict: executing`, `awaiting_review`, `blocked-on-conflict`, `blocked-on-ci`, `blocked-on-review`, `blocked-on-validate`, `mergeable`, `wf-feedback: failed`, … |
| `age` | duration since last activity | derived; sort default |
| `pr` | int (linked) + mergeability badge | `task_prs.pr_number` + `task_mergeability.derived_mergeability` |
| `account` | string | repo's `claude_account` or default |

**Side rail — recent events feed (last ~20):**
Source: `events` ORDER BY `created_at` DESC LIMIT 20.

| Field | Type |
|---|---|
| `entity_type` | enum: `plan`, `task`, `step`, `run`, `github`, `schedule`, `validate`, `review` |
| `action` | enum varies by entity: `created`, `dispatched`, `started`, `completed`, `failed`, `cancelled`, `escalated_to_operator`, `pr_opened`, `pr_merged`, `tick`, … |
| `task_id` | uuid (linked to /tasks/:id) |
| `created_at` | timestamp |

**Escalation banner (if any):**
Tasks with a `task.escalated_to_operator` event published in the last 24h, not yet acknowledged. One line each linking to the task.

---

### Task Detail page — what the operator sees

Route: `/tasks/:task_id`.

**Header:**
- Title, repo (link to GitHub repo), `derived_status` (large badge), plan link, created/started/completed timestamps.
- Account routing badge — which Claude account this task bills (`personal` / `zephyr` / etc., per ADR-0055).

**PR strip (if `task_prs` row exists for this task):**
Source: `task_prs` + `task_mergeability`.

| Field | Type | Notes |
|---|---|---|
| `pr_number` | int (link to GitHub) | |
| `branch` | string | from `task_prs.branch` |
| `head_sha` | string (short) | `task_mergeability.head_sha` |
| `ci_conclusion` | enum: `success` / `failure` / `pending` / null | `task_mergeability.ci_conclusion` |
| `review_decision` | enum: `approved` / `changes_requested` / `needs-more-info` / null | |
| `validate_decision` | enum: `pass` / `fail` / null | |
| `pr_conflicting` | bool | |
| `derived_mergeability` | enum: `pending` / `blocked-on-conflict` / `blocked-on-ci` / `blocked-on-review` / `blocked-on-validate` / `mergeable` | the single source of truth for "where this PR is stuck" |

**Workflow runs timeline:**
Source: `workflow_runs WHERE task_id = :id` ORDER BY `created_at`.

| Field | Notes |
|---|---|
| `workflow_id` | e.g. `wf-quick`, `wf-review`, `wf-ci-fix`, `wf-feedback`, `wf-conflict`, `wf-validate` |
| `status` | `queued` / `running` / `completed` / `failed` |
| `started_at`, `completed_at` | duration shown |
| step count | `workflow_run_steps` rows under this run |

**Selected run drill — steps:**
Source: `workflow_run_steps WHERE run_id = :run_id` ORDER BY `created_at`.

| Field | Notes |
|---|---|
| `role_id` | which role ran the step |
| `status` | `running` / `completed` / `failed` |
| `started_at` / `completed_at` | duration |
| `output` (jsonb) | the `StepOutput` envelope — `summary`, `decision`, `commit_sha`, artifacts. **Expandable.** |
| `error` (text) | if failed; **expandable**. |
| token usage | from per-step token-usage tracking (sibling's PR; persisted) — input / output / total per step |

**Events feed (filtered to this task):**
Same shape as overview side rail, filtered `WHERE task_id = :id`.

**Action affordances** (each backed by an API endpoint or event insert):

| Action | Backed by | Show when |
|---|---|---|
| Cancel task | INSERT `events (entity_type='task', action='cancelled', task_id, payload={"reason"})` | `derived_status` is non-terminal |
| Open PR on GitHub | deeplink | `task_prs` row exists |
| Retry failed step | (existing dispatcher path; specifics TBD) | last step's status = `failed` |
| Override review | ADR-0042 `validate.override` — **confirm exists before wiring** | review is `changes_requested` or `needs-more-info` |
| Acknowledge escalation | INSERT acknowledgement event | a `task.escalated_to_operator` exists for this task |

---

### API endpoints the dashboard needs (sketch)

```
GET  /api/v1/dashboard/overview
  → { tasks: [...non-terminal rows...],
      events: [...last N system events...],
      fleet: { workers_running, autoscaler_last_tick, scheduler_last_tick },
      spend_24h: [{account, tokens, usd_est}],
      escalations: [{task_id, repo, title, escalated_at}] }

GET  /api/v1/dashboard/tasks/:task_id
  → { task, pr, runs: [{run, steps: [...]}], events: [...], spend: {...} }

WS   /ws/events       (subscribe to task/run/step/github lifecycle events)
POST /api/v1/tasks/:task_id/cancel   { reason }     (existing event insert path)
POST /api/v1/tasks/:task_id/retry    {}             (existing dispatcher path)
POST /api/v1/reviews/override        { task_id, head_sha, decision } (ADR-0042 — confirm)
```

These are aggregation/UX endpoints; the underlying data is already in the views/tables above.

## Risks / unknowns

- **`validate.override` / "override review" surface** — referenced as ADR-0042 in memory; confirm the endpoint shape exists before wiring the action. **Abort** that affordance if it doesn't; surface the others first.
- **WebSocket event payload shape** — bunkhouse's `/ws/workers` emits worker-level events; Treadmill's are at a finer grain (plan/task/run/step). Worth a small ADR if the shapes don't line up cleanly with what `EventBus.publish` already emits.
- **Per-account spend computation** — depends on token-usage persistence (sibling's wave-1 PR — confirm it's landed and the join works on `workflow_run_steps`).
- **Claude Design canvas drift from the data shapes here** — if the canvas mocks fields that don't exist, lift will mis-render. **Mitigation:** I review the bundle before wiring, flag any shape-drift, and we adjust before code lands.
- **Single-operator-local scope discipline** — easy to scope-creep into auth/multi-user. Out of scope. v1 ships behind `localhost`.

## Decisions captured during execution

- **Phasing split.** PR A ships only the visual layer + design system +
  both pages against mock data, containerized. PR B wires the API
  aggregation endpoints (`routers/dashboard.py`), `/ws/events`, and the
  action affordances. Splits the risk surfaces: PR A is visual-only and
  can land without an API change; PR B is API + wiring without
  re-relitigating the design.
- **Direction C ("Console v2") is canonical.** The Claude Design canvas
  also has A ("Cousin") and B ("Console"); per chat-2 of the handoff
  bundle, both are predecessors only. C alone is what got cleaned up to
  ship.
- **Dashboard host port = 5174**, not 3000 or 5173. Sits next to Vite's
  dev-server port (5173) so a `npm run dev` against the source can
  coexist with the containerized build during phase-2 wiring work.
- **No deploy-watcher hook in PR A.** `services/dashboard/**` PR merges
  do not (yet) recreate the dashboard container. Acceptable for phase 1
  since the dashboard is mock-data-only; phase 2 adds the hook.
- **`fmt` is the only place numeric formatting happens.** A pinned
  test suite (`src/design/fmt.test.ts`) covers the breakpoints between
  unit suffixes so "just shave a digit" can't drift the UI's numeric
  vocabulary silently across pages.

## Post-mortem

**What shipped (PR A, hands-off scaffold + visual layer):**
- `services/dashboard/` — Vite 7 + React 19 + TS 5.9 + Tailwind 4 +
  TanStack Query 5 + react-router 7, multi-stage Docker (Node 22-alpine →
  nginx:alpine).
- Design system: `tokens.css`, `fmt.ts`, `Metric`, `StateBadge`,
  `Button`, `PageLayout` (with embedded loading/error states),
  `Lifecycle`, `DataTable`, `ConnectionAffordance`, `chrome.tsx`
  (RepoCell · AccountPill · WorkflowChip · PipelinePill · MetricChip).
- Two pages: Overview (Blocked / In-flight / Hopper buckets, escalation
  strip, fleet + spend top strip, filter row, events tail) and Task
  Detail (Lifecycle stepper + Iteration track hero + Blocking panel +
  PR strip + Action bar + IterationDetail drilldown + right rail with
  cost / repo docs / per-task event tail / cancel modal).
- Mock data layer + `useLiveSim` simulating the WS freshness signal.
- Local-adapter integration: `treadmill-local up` builds + runs the
  dashboard container, advertised at `http://localhost:5174`.
- Tests: 10 `fmt`-formatter assertions, all green.
- `services/dashboard/AGENT.md` documents the component for future work.

**Bundle handoff worked smoothly.** The Claude Design bundle's
`treadmill-overview-v2.jsx` / `treadmill-taskdetail-v2.jsx` translated
cleanly to TSX with `window.X` globals → ES imports. Shape-check across
the bundle's mocked fields vs this plan's data spec found one minor
delta: bundle's tasks carry `repo_mode` (`'conform' | 'adapt'`), trivially
resolvable in the phase-2 aggregation endpoint.

**Build + bundle size:** Vite production build = 343 kB JS (102 kB
gzip) + 9 kB CSS. No tree-shaking warnings.

**Open follow-ups (carried into PR B):**
- API aggregation endpoints (`routers/dashboard.py`).
- WebSocket `/ws/events` subscription.
- Swap each `queries.ts` `queryFn` body for `fetch(...)`.
- Wire real action affordances (Cancel / Ack-escalation / Retry / Override).
- Deploy-watcher hook for `services/dashboard/**` PR merges.
- Confirm ADR-0042 `validate.override` surface before wiring the
  "override review" affordance — abort it if it doesn't exist.

## PR B task breakdown (worker-dispatchable)

The follow-up work splits along clean file seams so most of it can run in parallel as Treadmill worker tasks (no merge conflicts between siblings). Each task below is sized for a single PR — bounded inputs, bounded outputs, one reviewer pass.

**Architectural anchor:** ADR-0056. Every dispatched worker should read it before touching `services/dashboard/` or `services/api/treadmill_api/routers/dashboard/`.

### Parallel-safe tasks (can dispatch concurrently)

Each one touches a distinct file. The API tasks deliberately split the router into per-endpoint files so concurrent PRs don't conflict.

| # | Task | File(s) touched | Inputs (data) | Output |
|---|---|---|---|---|
| B1 | `GET /api/dashboard/overview` aggregation endpoint | `services/api/treadmill_api/routers/dashboard/overview.py` (new) + router registration in `routers/__init__.py` | Existing `tasks` / `events` / `accounts` / `workers` / `escalations` tables; `derived_status` + `last_activity` from `task_status` view | JSON payload matching `src/api/types.ts` `useOverview` queryFn return — `{accounts, fleet, escalations, tasks, bucketCounts, events}` |
| B2 | `GET /api/dashboard/tasks/:taskId` task-detail endpoint | `services/api/treadmill_api/routers/dashboard/task_detail.py` (new) | `tasks`, `task_prs`, `workflow_runs`, `workflow_run_steps` (joins by task_id) | Payload matching `useTaskDetail`'s `TaskDetail` type — `{task, runs}` (with steps nested) |
| B3 | `GET /api/dashboard/repos/:repo/docs` repo-docs endpoint | `services/api/treadmill_api/routers/dashboard/repo_docs.py` (new) | ADR-0054 context-docs S3 bucket — `arch.md` head + `plans/` index | `{arch, plans, last_updated}` matching `useRepoDocs` |
| B4 | `POST /api/tasks/:id/cancel` action endpoint | `services/api/treadmill_api/routers/dashboard/actions.py` (new — both cancel + ack live here) | Body `{reason}`; existing event-insert path | Inserts `events (entity_type='task', action='cancelled', task_id, payload={"reason"})`; idempotent; returns 202 with the new event id |
| B5 | `POST /api/tasks/:id/ack-escalation` action endpoint | same file as B4 | Existing event-insert path | Inserts acknowledgement event; idempotent |
| B6 | Deploy-watcher hook for `services/dashboard/**` | `tools/local-adapter/treadmill_local/deploy_watcher.py` + `runtime.py` (add `recreate_dashboard_container`) | Existing API-recreate path is the template | A merged `services/dashboard/**` PR triggers a `docker build` + `docker rm -f treadmill-dashboard` + start, mirroring the API path; deploy-watcher log shows it; the AGENT.md follow-up is removed |
| B7 | `validate.override` surface audit | `docs/adrs/0042-*.md` (read) + new ADR or comment if absent | ADR-0042 references | A one-paragraph confirmation that the surface exists (with endpoint shape) **or** a one-paragraph rationale for dropping the "override review" affordance from the UI. PR B8 reads this output. |

### Sequenced tasks (must wait on parallel set)

Each of these touches `src/api/queries.ts` or `src/api/sim.ts` and so cannot run alongside its peers without conflicts. Dispatch sequentially; they're each small.

| # | Task | File(s) touched | Depends on | Output |
|---|---|---|---|---|
| B8 | Swap `useOverview`, `useTaskDetail`, `useRepoDocs` to `fetch(...)` | `src/api/queries.ts` | B1, B2, B3 merged | Each `queryFn` body becomes a `fetch('/api/dashboard/...').then(r => r.json())`; `mock.ts` imports removed for these three; `_simAdvance` import stays (still used by `sim.ts`); page components unchanged |
| B9 | Wire `useCancelTask` + `useAcknowledgeEscalation` mutations to real endpoints | `src/api/queries.ts` | B4, B5 merged | Mutation `mutationFn` posts to the real endpoint; existing optimistic-update + rollback logic stays |
| B10 | Wire "override review" action (conditional) | `src/pages/TaskDetail.tsx` + `src/api/queries.ts` | B7 outcome | If B7 confirmed the surface: render the action with real wiring. If B7 dropped it: remove the `needsReviewOverride` branch from `ActionBar` |
| B11 | Real WebSocket subscription replacing `useLiveSim`'s `setInterval` | `src/api/sim.ts` + new `services/api/treadmill_api/routers/dashboard/ws.py` | B1 + B2 merged (events shape stable) | Server-side: subscribe to EventBus, relay events over WS with backpressure; client-side: `useLiveSim` opens a WS to `/ws/events`, updates `lastUpdated` + `flashIds` from real pushes, falls back to `mode='polling'` on disconnect |

### Out-of-PR-B follow-ups

Captured here so the worker dispatcher doesn't pick them up by accident:

- **Auth.** Single-operator-local v1 has none. A future ADR governs multi-tenant auth; that's not PR B.
- **Lift more bunkhouse routes.** ADR-0056 explicitly defers this until an operator reaches for one.
- **Server-side event-tail `task_id` filter.** B1 returns all events; client filters. Server-side filter is a perf optimization when volume rises.

### Dispatcher hints

- All B1–B6 worker tasks should carry `claude_account: personal` (the operator's own dashboard work bills personal, not zephyr/bunkhouse) — see ADR-0055.
- All tasks should reference ADR-0056 and `docs/dashboard/DESIGN.md` in their initial prompt.
- B7 is read-only; can run on any account.
- The seven parallel tasks (B1–B7) collapse PR B from "one bigass PR" to a fan-out of seven small PRs that merge in any order, plus three small sequenced ones. Realistic wall-clock with the autoscaler at default capacity: ~one Treadmill ralph-loop per task = ~25–40 min/task, four in flight at once = PR B lands inside half a day instead of a focused multi-day session.
