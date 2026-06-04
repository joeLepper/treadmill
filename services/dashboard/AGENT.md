# services/dashboard

Treadmill operator dashboard. Single-operator-local React SPA, served from
a static nginx container alongside the API in dev-local.

**Status:** v1, phase 1 (visual layer + mock data). Phase 2 wires live data
via `services/api/treadmill_api/routers/dashboard.py` aggregation endpoints
plus `/ws/events`.

**Plan & design briefs:**
- Plan: `docs/plans/2026-05-26-treadmill-dashboard-v1.md`
- Design rules: `docs/dashboard/DESIGN.md`

## What's in here

```
src/
  design/      one-and-only-one primitives — StateBadge, DataTable,
               PageLayout, Lifecycle, Metric, ConnectionAffordance, etc.
  api/         types.ts (canonical shapes), mock.ts (in-process fixture),
               queries.ts (TanStack Query hooks), sim.ts (freshness sim)
  pages/       Overview.tsx (the / route), TaskDetail.tsx (/tasks/:id)
  index.css    Tailwind v4 entry + tokens.css import
  main.tsx     QueryClientProvider + BrowserRouter shell
  App.tsx      Routes
```

The handoff bundle from Claude Design (`treadmill-overview-v2.jsx`,
`treadmill-taskdetail-v2.jsx`, `treadmill-format.jsx`,
`treadmill-system.jsx`, `treadmill-mock.jsx`) is the source-of-truth
reference. Direction C ("Console v2") is the canonical visual direction —
A and B from the canvas exist only as predecessors.

## Mandatory rules (from DESIGN.md, enforced here)

1. **One `<StateBadge>`** for every entity status. No per-entity badge
   variants — the entity type is communicated by an icon prefix or the
   container, never by a separate `<TaskBadge>` / `<PRBadge>`.
2. **One `<DataTable>`** for every listing. Sortable hint, sticky headers,
   density consistent across pages.
3. **One `<PageLayout>`** wrapper for every page. It owns its own
   loading / error / not-found states. Detail pages do not reinvent the
   wrapper, ever.
4. **Lifecycle above the fold** on every detail page (`<Lifecycle>`).
5. **Section order driven by what's blocking** — blocked content rises to
   the top, not the bottom (BlockingPanel above PRStrip on TaskDetail).
6. **Red reserved for needs-attention.** Cancelled / superseded /
   abandoned are muted gray, not red.
7. **Closed semantic palette.** Tones come from `tones.*` helpers in
   `design/fmt.ts`. No hand-rolled `s > 600 ? 'danger' : ...` at call
   sites.
8. **Connection-freshness affordance always visible** —
   `<ConnectionAffordance>` in every page's top bar. Stale data never
   masquerades as live data.

## Phase 2 swap (live data)

The page components consume `src/api/queries.ts`; they never reach into
`mock.ts` directly. Migration is a per-hook `queryFn` body swap — replace
the mock call with a `fetch('/api/...').then(...)` and the page renders
unchanged.

Per-hook migration status:

- `useOverview` — **MIGRATED (PR-B8)** → `GET /api/v1/dashboard/overview`
  (filters `repo`/`bucket`/`account`/`q` forwarded as query parameters).
- `useTaskDetail` — **MIGRATED (PR-B8)** → `GET /api/v1/dashboard/tasks/:taskId`.
- `useRepoDocs` — **MIGRATED (PR-B8)** → `GET /api/v1/dashboard/repos/:repo/docs`.
- `useCancelTask` — **MIGRATED (PR-B9)** → `POST /api/v1/dashboard/tasks/:task_id/cancel`.
- `useAcknowledgeEscalation` — **MIGRATED (PR-B9)** → `POST /api/v1/dashboard/tasks/:task_id/ack-escalation`.
- `/ws/events` — WebSocket migration lands in **PR-B11** (currently driven by `sim.ts`).

## Running locally

`treadmill-local up` builds the `treadmill-dashboard:dev` image (multi-
stage Node 22-alpine → nginx:alpine) and brings up the container on host
port **5174**. Visit `http://localhost:5174`. For hot-reload work, run
the Vite dev server in this directory instead:

```
cd services/dashboard
npm install
npm run dev   # → http://localhost:5173
```

The dev server proxies `/api` → `http://localhost:8088`
(`VITE_DEV_API_URL` overrides for nginx-fronted setups).

## Tests

`npm run test` runs Vitest with jsdom + `@testing-library/jest-dom`. The
formatters in `design/fmt.ts` are the highest-leverage thing to test —
every metric on the dashboard routes through them. A regression there
silently drifts the UI's numeric vocabulary across pages.

## Known follow-ups

- Right-rail event-tail filtering is client-side over the global feed; a
  server-side `task_id` filter on `/api/dashboard/events` will be cheaper
  once the event volume rises.
- Mock data covers two canonical pages only — bunkhouse's ~25 routes are
  deliberately not ported. Lift more routes from bunkhouse when the
  operator actually reaches for them.

## Recent changes

- **UI-fix — triage finding `0b1dbe45`** — `deriveLifecycleIdx` in `src/design/Lifecycle.tsx` was falling through to the default `return 0` (REGISTERED) for composite `derived_status` strings such as `pr_opened (wf-conflict: failed)`, because none of the enumerated branches matched. The stepper therefore highlighted step 01 amber for tasks that obviously had an open PR and an active workflow run. Added a branch that maps any status starting with `pr_opened` or containing the `(wf-` workflow-run suffix to lifecycle index 1 (EXECUTING) before the final fallback. Regression pinned by `src/design/Lifecycle.test.tsx`.
- **UI-fix — triage finding `7e4ab8f6` (manual ship)** — Added `aria-label` to each `ack` Button in the Overview escalation strip (`src/pages/Overview.tsx`), formatted as `Acknowledge {esc.title} escalation`. Threaded `aria-label` through the `Button` design primitive (`src/design/Button.tsx`) since it didn't previously forward arbitrary HTML attributes. Satisfies WCAG 1.3.1 by making each ack button's task relationship determinable to assistive tech (previously all 49 buttons read as bare "ack"). Authored by the v1.3 `role-ui-triage` worker (task `d3ac6992`) but the cybernetic loop's Playwright-validation gate proved unsatisfiable pre-merge — gate probed the deployed bundle at `http://treadmill-dashboard:80/`, which still had the bug — so the task was cancelled and the diff applied manually. See the follow-up ADR-0061 amendment for the gate-strategy fix.
- **UI-fix — triage findings `3fb3291d` + `42e9cad2`** (v1.3 `wf-ui-triage` run `09088b01-411c-4bc5-adf5-10bdd6144f78`). `useRepoDocs(repo)` in `src/api/queries.ts` now passes `enabled: !!repo` to `useQuery`, so the hook stays idle on an empty repo string instead of firing a `/api/v1/dashboard/repos//docs` request (finding `3fb3291d`); a third case in `src/api/queries.test.tsx` pins the no-fetch behavior. The `NAV` array in `src/design/PageLayout.tsx` drops the three phantom entries (`/plans`, `/events`, `/repos`) that pointed at routes `App.tsx` never registered — only `/` (Overview) and `/tasks` remain (finding `42e9cad2`). Both fixes enforce DESIGN.md rule F ("Delete commented-out routes and phantom pages"). Unused `GitBranch` / `Zap` / `Terminal` icons trimmed from the local `lucide-react` import.
- **ADR-0061 triage labeling UI** — new `/triage` route (`src/pages/TriageLabeling.tsx`) — a flip-through labeler that walks the unlabeled triage queue one finding at a time. Left column: screenshot (lazy `<img>`; S3 URIs fall back to a labeled link until a presign endpoint lands), observation, evidence_pointer, proposed_resolution. Right column: the four ADR-0061 label questions — Yes/No/Skip for `is_real_bug`; high/medium/low/Skip for `severity`; category dropdown + Skip; Yes/No/Skip for `fix_in_dsl` — plus a free-text notes textarea and a Submit button. "Skip" leaves the field `null` because null is itself a signal per the v1 prompt. New hooks in `src/api/queries.ts`: `useUnlabeledFindings()` (`GET /api/v1/triage/findings?label_is_real_bug=null&limit=50`) and `useLabelFinding()` (`POST /api/v1/triage/findings/:id/label`, with optimistic removal of the labeled finding from the `['triage', 'unlabeled']` cache so the UI advances without waiting for refetch). New `TriageFinding` + `TriageLabelInput` types in `src/api/types.ts` mirroring the Pydantic schema in `services/api/treadmill_api/schemas/triage_finding.py`. Route registered in `src/App.tsx`. Page uses the existing `PageLayout` / `StateBadge` / `Button` primitives per DESIGN.md mandatory rules — no new chrome introduced.
- **Fix — nginx reverse proxy** (`services/dashboard/nginx.conf`). Added a
  `/api/` location block proxying to `http://treadmill-api:8088` (the
  api container by docker-network DNS) and carrying WebSocket
  Upgrade/Connection headers so `/api/v1/dashboard/ws/events` rides
  the same prefix. Without this, the SPA fallback caught every
  `/api/v1/...` fetch from `queries.ts` and returned `index.html`,
  so `await res.json()` threw and Overview rendered as a blank shell.
- **PR-B11** — `src/api/sim.ts`'s `useLiveSim` now drives a real
  WebSocket subscription against `${WS_BASE}/api/v1/dashboard/ws/events`
  (derived from `window.location` — `wss:` when the page is on
  `https:`, `ws:` otherwise). Mode flips to `'ws'` on `onopen`,
  `'disconnected'` on close/error, with exponential reconnect backoff
  (1 s → 2 s → 4 s, capped 30 s). `event` messages with a `task_id`
  populate `flashIds` for 1.5 s; `lastUpdated` refreshes on every
  incoming frame plus the existing 1-second clock interval. Hook's
  return shape unchanged. Tests in `src/api/sim.test.tsx` stub
  `window.WebSocket` via `vi.stubGlobal` and cover open→`'ws'`,
  event→`flashIds`, lastUpdated-on-message, and
  close→`'disconnected'`+reconnect backoff.
- **PR-B9** — Swapped `useCancelTask` and `useAcknowledgeEscalation`
  mutation bodies from `mock.ts` to live `fetch` against
  `POST /api/v1/dashboard/tasks/:task_id/{cancel,ack-escalation}`.
  Non-2xx surfaces as a thrown `Error` carrying the HTTP status. The
  optimistic-update + rollback machinery on `useAcknowledgeEscalation`
  is preserved unchanged (it manipulates TanStack Query cache and was
  never tied to the mock). Mutation shapes unchanged so callsites
  don't move. Added cases to `src/api/queries.test.tsx` pinning URL,
  body, optimistic update, rollback on failure, and error surfacing.
- **PR-B10** — Removed the `override·review` button from `ActionBar` in
  `src/pages/TaskDetail.tsx`. B7's audit
  (`docs/dashboard/validate-override-surface.md`) confirmed ADR-0042's
  `validate.override` is internal-only with no callable HTTP surface, and
  the prior render condition conflated `validate.override` with
  `review.override` (separate event domains, both internal-only). Regression
  guarded by `src/pages/TaskDetail.test.tsx`.
