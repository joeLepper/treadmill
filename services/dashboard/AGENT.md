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

Endpoints we'll need on the API side:

- `GET /api/dashboard/overview` — `{accounts, fleet, escalations, tasks, bucketCounts, events}`
- `GET /api/dashboard/tasks/:taskId` — `{task, runs}`
- `GET /api/dashboard/repos/:repo/docs` — `{arch, plans, last_updated}`
- `POST /api/tasks/:id/cancel` — `{reason}`
- `POST /api/tasks/:id/ack-escalation`
- `GET /ws/events` — WebSocket; pushes the event-tail items + flash hints.

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
