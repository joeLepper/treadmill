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
               queries.ts (TanStack Query hooks), sim.ts (freshness sim),
               review_queries.ts (per-kind ADR-0070 hooks: useReviewNext,
               useReviewStats, useLabelReviewRow), review_types.ts
               (StatsResponse wire shape).
  review/      ADR-0070 pre-labeled review-queue substrate:
               types.ts (ReviewRow / ReviewKindViewer / ReviewLabelInput),
               useReviewKeyboard.ts (closed shortcut set), ConfidenceStrip.tsx
               (per-kind bucket + accuracy strip), FlipThroughLayout.tsx
               (the one-row-at-a-time chrome), registry.ts (auto-discovers
               per-kind viewers via import.meta.glob), viewers/ (per-kind
               viewers, one .tsx per kind; substep 1.3 ships zero viewers).
  pages/       Overview.tsx (the / route), TaskDetail.tsx (/tasks/:id),
               TriageLabeling.tsx (the ADR-0061 precedent the review/
               chrome generalizes; not yet refactored onto it),
               ReviewKind.tsx (the /review/:kind route — single dynamic
               page that every per-kind queue rides via the registry).
  index.css    Tailwind v4 entry + tokens.css import
  main.tsx     QueryClientProvider + BrowserRouter shell
  App.tsx      Routes (/, /tasks/:taskId, /triage, /review/:kind)
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

- **ADR-0070 substep 2 step 2 — register triage-finding viewer** — New
  `src/review/viewers/triage-finding.tsx` default-export viewer component
  for the triage-finding review queue. Viewer auto-discovered by the
  kind-to-component registry via `import.meta.glob('./viewers/*.tsx',
  { eager: true })`; substep 1's auto-discovery wire-up picks it up at
  build time and registers it at kind='triage-finding'. The viewer follows
  the legacy TriageLabeling.tsx two-column layout: left column shows the
  evidence (screenshot, observation, evidence_pointer, proposed_resolution)
  plus an LLM recommendation card (confidence + rationale); right column
  shows the label form (is_real_bug Yes/No/Skip, severity high/medium/low/Skip,
  category dropdown + Skip, fix_in_dsl Yes/No/Skip, notes textarea) and
  submit button. The legacy `/triage` route in `src/App.tsx` now redirects
  to `/review/triage-finding`, preserving existing bookmarks. Legacy
  `pages/TriageLabeling.tsx` and `pages/TriageLabeling.test.tsx` deleted;
  the viewer + substep 1's `FlipThroughLayout` chrome replace the page.
  TODO comments added above `useUnlabeledFindings` and `useLabelFinding`
  in `src/api/queries.ts` marking them for removal in substep 4 when the
  legacy `/api/v1/triage/` endpoints are deprecated. Viewer test coverage
  in `src/review/viewers/triage-finding.test.tsx` pins: evidence field
  rendering, LLM card rendering, accept path (label='true'), reject path
  (label='false'), skip path (label='null'), draft reset on row change,
  and kind-specific fields (label_severity, label_category, label_fix_in_dsl).
  App redirect test in `src/App.test.tsx` verifies `/triage` mounts the
  new framework chrome (`FlipThroughLayout` title) not the legacy heading.

- **ADR-0070 substep 4.3 — DSPy variant PR review queue** — new `/review/dspy-variant-pr` route backed by a default-export viewer at `src/review/dspy_variant_pr.tsx`. The viewer follows the TriageLabeling.tsx two-column layout: left column shows judge_role, PR link (source_pr_number → source_pr_url), created_at, score badges (current/variant/improvement), patch diff in `<pre>`, and corpus S3 URI; right column shows the LLM recommendation card (llm_label + llm_confidence badge, llm_rationale, llm_prompt_version + llm_model footer) and the label form (merge/revise/drop/skip verdict buttons, notes textarea, conditional override_reason field that becomes required and highlighted when the operator's verdict differs from llm_label). Submit is disabled until a verdict is chosen and any required override_reason is provided. New types in `src/api/types.ts`: `DspyVariantPrLabel`, `DspyVariantPrConfidence`, `DspyVariantPrRow`, `DspyVariantPrLabelInput`. New hooks in `src/api/queries.ts`: `useDspyVariantPrQueue(limit?)` (`GET /api/v1/review/dspy-variant-pr/next?limit=…`), `useDspyVariantPrStats()` (`GET /api/v1/review/dspy-variant-pr/stats`), `useLabelDspyVariantPr()` (`POST /api/v1/review/dspy-variant-pr/:id/label` with optimistic remove-on-mutate mirroring useLabelFinding). Route registered in `src/App.tsx` as a static path BEFORE the dynamic `/review/:kind` catch-all. Test coverage in `src/review/dspy_variant_pr.test.tsx` pins: render with data (judge_role + PR number visible), empty-state copy, submit-with-override_reason when disagreeing, and disabled-submit guard when override_reason absent.

- **UI-fix — triage finding `300648e9`** — `src/pages/TriageLabeling.tsx`
  was rendering without a `<ConnectionAffordance>` in the top bar,
  violating DESIGN.md mandatory rule #8 ("connection-freshness affordance
  always visible"). Imported `useLiveSim` from `../api/sim` and
  `ConnectionAffordance` from `../design/ConnectionAffordance`, called
  `const sim = useLiveSim()` inside the page, and threaded
  `freshness={<ConnectionAffordance mode={sim.mode} lastUpdated={sim.lastUpdated} />}`
  through to `<PageLayout>`. Matches the pattern already in
  `Overview.tsx` and `TaskDetail.tsx`. Regression pinned by
  `src/pages/TriageLabeling.test.tsx` (mocks `useLiveSim` to return
  `mode: 'ws'` and asserts the "Live" affordance text reaches the DOM).
- **ADR-0070 substep 1.4 — /review/:kind route + auto-discovery wire-up** —
  new `src/pages/ReviewKind.tsx` mounted at `/review/:kind` in
  `src/App.tsx` (registered BEFORE the `*` fallback so unknown
  `/review/*` paths reach the in-page unknown-kind panel rather than
  bouncing back to `/`). The page reads `useParams<{ kind }>()`, calls
  `getViewer(kind)` from `src/review/registry.ts`, and when the kind is
  unregistered renders a 404-style panel pointing at the registry
  contract. When the kind IS registered the page wires three new hooks
  in `src/api/review_queries.ts` — `useReviewNext(kind, *, limit?)`
  (`GET /api/v1/review/<kind>/next?limit=…`, queryKey
  `['review', kind, 'next']`, staleTime 3s), `useReviewStats(kind)`
  (`GET /api/v1/review/<kind>/stats`, queryKey
  `['review', kind, 'stats']`, staleTime 15s), and `useLabelReviewRow(kind)`
  (`POST /api/v1/review/<kind>/<id>/label` carrying `ReviewLabelInput`,
  optimistic update drops the labeled row from the unlabeled cache so
  the chrome advances without a refetch — mirrors `useLabelFinding`
  pattern in `queries.ts:145-182` — and invalidates the stats key on
  settle). The page hands the first unlabeled row + the LLM stats to
  `FlipThroughLayout` and pipes `onLabel` through the mutation; per-bucket
  count breakdown is empty until the API grows it (the chrome's
  `ConfidenceStrip` defaults missing buckets to zero, so the strip still
  renders). New wire-shape file `src/api/review_types.ts` mirrors
  `treadmill_api.services.review_stats.StatsResponse`. Test coverage:
  `src/api/review_queries.test.tsx` pins the three hooks' URLs +
  optimistic-update-and-rollback against the next cache;
  `src/pages/ReviewKind.test.tsx` pins the unknown-kind panel
  (no fetch issued), the registered-kind happy path (stub viewer
  resolves via `vi.mock('../review/registry', …)`), and the
  `space → POST /api/v1/review/<kind>/<id>/label` one-keystroke confirm
  path carrying the LLM's label as the operator's verdict.
- **ADR-0070 substep 1.3 — shared review-queue chrome** — new
  `src/review/` substrate that generalizes the `TriageLabeling`
  flip-through page into a reusable surface every "operator
  sanity-checks LLM" queue can ride on. `types.ts` defines the
  `ReviewRow<TCandidate, TLlm>` shape plus the
  `ReviewLabelInput` write contract. `useReviewKeyboard.ts` is the
  closed shortcut set (`space`/`x`/`s`/`?`/`j`/`k`) with the
  `<input>`/`<textarea>`/`[contenteditable]` focus guard so typing into
  the notes field doesn't trigger shortcuts. `ConfidenceStrip.tsx` is
  the pure-presentation header strip (high/medium/low buckets + optional
  per-kind accuracy pill). `FlipThroughLayout.tsx` is the chrome itself
  — it wraps `PageLayout`, owns the keyboard handler, dispatches a
  `review:request-override-focus` custom event on `x` so per-kind
  viewers can focus their override-reason field, and renders the
  per-kind body via the registry-selected viewer. `registry.ts`
  auto-discovers viewers via `import.meta.glob('./viewers/*.tsx', {
  eager: true })`; substep 1.3 ships zero real viewers (just
  `viewers/_README.txt` documenting the contract), so every lookup
  returns null until substep 2 lands `architect-gold`. None of the
  existing pages (`TriageLabeling.tsx` included) consume this substrate
  yet — the refactor lands in substep 2's per-kind work. Test coverage
  pins the shortcut mappings, the input-focus guard, the disabled-state,
  the empty-queue copy, the space-to-accept one-keystroke path, the
  accuracy-pill rendering, and the empty-registry invariant.
- **UI-fix — triage finding `71ed396b`** — Wired the `open·pr` Button in `ActionBar` (`src/pages/TaskDetail.tsx`) to its missing `onClick`. It now opens `https://github.com/{task.repo}/pull/{task.pr.pr_number}` in a new tab via `window.open(..., '_blank')`, satisfying DESIGN.md Page 2's required "Open PR on GitHub (deeplink)" affordance — previously the button rendered with the `ExternalLink` icon but no navigation, so the operator could see the affordance but not use it. Regression guarded by a new `ActionBar` test in `src/pages/TaskDetail.test.tsx` that spies on `window.open` and asserts the URL/target.
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
