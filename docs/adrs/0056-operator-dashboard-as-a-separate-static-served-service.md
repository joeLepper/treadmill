# ADR-0056: Operator dashboard as a separate static-served service

- **Status:** accepted
- **Date:** 2026-05-27
- **Related:** ADR-0055 (per-account Claude credentials — account-pill chrome in the UI consumes this), ADR-0050 (conform/adapt mode — RepoCell chrome surfaces it), ADR-0016 (compute stays local in dev-local — dashboard is one more local container), ADR-0023 (operator-post-deploy actions — dashboard surfaces them)
- **Plan:** `docs/plans/2026-05-26-treadmill-dashboard-v1.md`
- **Design brief:** `docs/dashboard/DESIGN.md`

## Context

Treadmill is converging on its first non-Treadmill bootstrap target. As the system runs more concurrent tasks across more accounts and repos, the operator's working surface — until now a mix of `treadmill-cli` invocations, `gh pr view`, raw event-log SQL, and ssh-into-the-worker introspection — stops scaling. We need a single visual surface that answers three operator questions in one glance:

1. **What needs me right now?** (escalations, blocked tasks, failing CI on a PR Treadmill opened.)
2. **What's the system doing?** (in-flight workflows, per-account spend, heartbeats.)
3. **Where am I in a given task's life?** (lifecycle position, iteration count, blocking reason, PR state.)

The same operator manages multiple Claude accounts and multiple repo modes (ADR-0050 conform vs. adapt), and the dashboard must surface those distinctions without bolting an account chip onto every existing CLI affordance one at a time.

We have a Claude-Design handoff bundle (direction C, "Console v2") and a sibling-project precedent: bunkhouse shipped a dashboard at `services/dashboard/` last year that we can lift the **type vocabulary** from while explicitly **not** inheriting its chrome (16 hand-rolled `min-h-screen bg-gray-100 p-8` wrappers drifted across detail pages — a failure mode we'd reproduce on autopilot if we copied bunkhouse's structure as-is).

## Decision

The dashboard ships as a **separate Treadmill service** at `services/dashboard/`:

- **Static React SPA** served by `nginx:alpine` — Vite 7 + React 19 + TypeScript 5.9 + Tailwind 4 + TanStack Query 5 + react-router 7. Multi-stage Docker (Node 22-alpine build → nginx serve).
- **Own container, own port** (host port **5174**), brought up by `treadmill-local up` alongside the API. No coupling to the API container's release cycle; static nginx in front, no SSR.
- **Single-operator-local v1.** No auth, no multi-tenant. Behind `localhost`. Multi-tenant auth is a deferred decision under a future ADR.
- **TanStack Query is the seam.** Page components consume `src/api/queries.ts`; the `queryFn` bodies are the only things that change between phases. Phase 1 calls in-process mock fixtures. Phase 2 swaps each `queryFn` body for a `fetch('/api/dashboard/...')` against new aggregation endpoints in `services/api/treadmill_api/routers/dashboard.py`, plus a `/ws/events` WebSocket subscription. **Page components do not change between phases.**

### Mandatory composition rules

The bunkhouse dashboard's failure mode was the proliferation of one-off variants: one `<TaskBadge>`, one `<PRBadge>`, one `<RunBadge>` — each subtly different in tone, height, label vocabulary. We pre-empt that by exactly four "one-and-only-one" rules, enforced as the API surface of the design system:

1. **One `<StateBadge>`** for every entity status. Entity type is conveyed by an icon prefix or container chrome, never by a separate badge variant.
2. **One `<DataTable>`** for every listing.
3. **One `<PageLayout>`** wrapper that owns its own loading / error / not-found states. Detail pages do not reinvent the page chrome.
4. **One `<Lifecycle>`** stepper, rendered above the fold on every detail page.

### Numeric & color discipline

- **Closed semantic palette.** Four tones (`danger` / `warn` / `ok` / `muted`) plus `info` reserved for chrome. Cancelled / superseded / abandoned are `muted`, never `danger` — red is reserved for needs-attention-now.
- **All numeric rendering goes through `design/fmt.ts`** or the `<Metric>` primitive. Hand-rolling `.toFixed(2)` or `s > 600 ? 'danger' : ...` at call sites is forbidden. Tones derive from `tones.*` helpers, never from inline ternaries.
- **Connection-freshness affordance always visible.** Stale data never masquerades as live data.

### Section order driven by what's blocking

A blocked task's page promotes the blocking panel above the PR strip. A clean task's page collapses the blocking panel away entirely. The operator's eye-path to "what's wrong" is the same regardless of which entity surfaces the problem.

### Direction C ("Console v2") canonical

The Claude Design canvas exposes three directions: A ("Cousin"), B ("Console"), C ("Console v2"). C alone is what we ship. A and B exist on the canvas only as predecessors of C.

### Two-PR phasing

- **PR A (#24, this ADR's anchor):** visual layer + design system + Overview + TaskDetail against mock data. Containerized. Lands without backend churn.
- **PR B (and possibly fanned-out worker tasks):** API aggregation endpoints, `/ws/events`, swap mock → fetch in `queries.ts`, wire live action affordances (Cancel · Ack · Retry · Override), deploy-watcher hook for `services/dashboard/**` PR merges.

## Alternatives considered

- **Embed the dashboard inside the API container.** Rejected: couples a UI release to an API release; precludes serving the dashboard from a CDN or behind nginx independently; conflates the API's Python+uvicorn runtime with a static-asset surface that needs neither.
- **Server-rendered HTML from the API (no SPA).** Rejected: every interaction round-trips; the WS/freshness affordance (rule #8) becomes synthetic; we'd reinvent component composition in Jinja. The dashboard's value is the live-feed feel of the iteration track + event tail, which needs a client-side reactive surface.
- **Lift the bunkhouse dashboard structure wholesale.** Rejected: the bunkhouse codebase contains the antipatterns we're explicitly trying to escape (16 hand-rolled page wrappers, per-entity badge variants). We adopt **type vocabulary** from bunkhouse (Tone, FreshnessMode, naming for status enums) but invert the composition rules.
- **Hand off the design system to a UI library (Radix / shadcn / Mantine).** Rejected for v1: the canonical surface is small (StateBadge, DataTable, PageLayout, Metric, Lifecycle, Button, chrome cells). A library imposes its own opinion on every one of those, and the discipline we're imposing here — closed palette, single-X rules, tokenized numerics — is precisely the discipline a generic library *doesn't* enforce.
- **One monolithic PR (visual + wiring).** Rejected: front-end review and API endpoint review benefit from separate review surfaces. The mock data layer is the seam; the queries.ts hooks are the contract; phase 2 is mechanically a queryFn-body swap. A single PR conflates two distinct review surfaces.
- **Skip the ADR; the plan + AGENT.md is enough.** Rejected (this very decision): the plan captures *what to build*; the AGENT.md captures *how to work in the directory*; neither captures *what we chose and why* in a way that future architects (human or worker) reading the repo cold will find. The "one StateBadge" rule, for example, is meaningless without the bunkhouse-failure context — which lives here.

## Consequences

### Good

- **First non-API service in the Treadmill repo.** Establishes the pattern for additional services (planner UI, validation visualizer, etc.) without coupling them to the API container's release cycle.
- **Phase-2 swap is mechanical.** Mock → fetch is a per-hook `queryFn` body change. Page components and design primitives don't move.
- **Worker-dispatchable.** PR B's task breakdown (in the plan) is structured as discrete, non-conflicting tasks — each one a self-contained endpoint or hook swap. Workers can run them in parallel.
- **Design-system discipline pinned by types and tests.** The `fmt` test suite + the `Tone` union + the closed `STATE_TONE` map make most antipattern drifts type-errors or test failures rather than visual review catches.
- **Bunkhouse lessons applied without bunkhouse code.** We get the type vocabulary's expressiveness without inheriting the chrome proliferation.

### Bad / trade-offs

- **One more container in `treadmill-local up`.** ~50 MB nginx + bundle; negligible for a single-operator-local workflow, but worth pinning here so we don't grow the local container fleet by accident.
- **Build dependency on Node.** A `treadmill-local up` from a clean machine now needs Node-in-Docker (handled by the multi-stage image) but contributors iterating with `npm run dev` need a host Node 22. The README documents the dev-server path.
- **Closed semantic palette is opinionated.** Operators who want a sixth tone for "warn-but-different" must extend `Tone` deliberately — which is the point, but adds friction to one-off requests.
- **Two-PR phasing means PR A ships visibly incomplete.** The Cancel/Ack/Retry buttons render but the mutations are mock-only until PR B. Pinned in `AGENT.md` and the plan's post-mortem so we don't ship PR A to an operator who'd reasonably expect the buttons to work end-to-end.

### Risks

- **Phase-2 endpoint shape drifts from the data spec.** If the API endpoints don't match `src/api/types.ts` exactly, the swap stops being mechanical. Mitigated by treating `src/api/types.ts` as the contract and reviewing the Python `routers/dashboard.py` payload shapes against it line-by-line.
- **Dashboard becomes the dumping ground.** Every UI request risks landing here. Mitigated by scope discipline: lift more pages from bunkhouse **when the operator actually reaches for them**, not preemptively (the bunkhouse dashboard has ~25 routes; we ship two).
- **Deploy-watcher gap.** Until PR B adds the `services/dashboard/**` recreate hook to the deploy-watcher, a merged dashboard PR doesn't refresh the running container — operator must `treadmill-local up` to pick it up. Pinned in `AGENT.md`.

## References

- Plan: `docs/plans/2026-05-26-treadmill-dashboard-v1.md` (includes PR B task breakdown)
- Design brief: `docs/dashboard/DESIGN.md` (closed-palette enumeration, page-by-page composition rules)
- Component AGENT: `services/dashboard/AGENT.md`
- PR A: https://github.com/joeLepper/treadmill/pull/24
- Bunkhouse reference (NOT a structural template, only a type-vocabulary one): `../bunkhouse/services/dashboard/`
