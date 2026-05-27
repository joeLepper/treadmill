# Treadmill Dashboard — design brief

**Audience:** Claude Design. You are designing the visual language and the two highest-frequency pages of a new operator dashboard for **Treadmill**, an event-sourced agentic-runner platform. The persistent-design-system source you are absorbing is the **bunkhouse** dashboard at `/home/joe/bunkhouse/services/dashboard/`. Treadmill is a deliberate evolution of bunkhouse, so the new dashboard should *descend from* the bunkhouse dashboard — recognizable as a cousin, not a clean-room rebuild. That said: bunkhouse's dashboard has known weak spots, and we are explicitly **not** inheriting them. This brief tells you what to copy faithfully, what to leave behind, and the few new things Treadmill needs that bunkhouse doesn't have.

---

## Who you're designing for

One operator. Returns to the dashboard dozens of times a day. Almost always asks one of two questions on arrival:

1. *"What's happening right now?"* — across all repos, which tasks are non-terminal, which are stuck and why, are workers/scheduler/autoscaler alive, has any health-bot escalated anything for me.
2. *"Why is this task here?"* — drill into one task to see its workflow runs, steps, outputs, errors, the PR it produced and where that PR is stuck, the cost it incurred, the buttons to intervene.

Everything else (submit work, manage repos, edit roles/skills/hooks, configure schedules) is **out of scope for v1**. The author skill + the existing API + GitHub UI cover those flows adequately. We are not redesigning bunkhouse one-for-one; we are extracting the operator-attention surface.

Single-operator local — no login in v1. If/when this is multi-tenant later, OAuth gets added then.

---

## Lineage: what to copy faithfully from bunkhouse

The dashboard's heart already exists in bunkhouse. These are the patterns that earn their keep. Use them as visual and structural anchors.

### 1. Lifecycle stepper above the fold on every detail page

**Reference:** `src/pages/TaskDetail.tsx:134-195` (`TaskLifecycleStepper`).

A horizontal stepper that collapses the entity's whole life into a fixed sequence of ~5 nodes — `Registered → Executing → Review → Merged → Validated` for a task — with the *current* node highlighted, prior nodes filled, future nodes ghosted. Critically: on failure the current node turns red with an X glyph rather than disappearing; the failure surfaces in the same component that shows success. This is the single most operator-respectful element in the dashboard and it answers *"where is this in its life?"* in 100ms.

Adopt this verbatim for Treadmill's `/tasks/:id` (and apply the same idea, with different stages, on plan-detail and workflow-run-detail when we get to them). **Hard rule: every Treadmill detail page must have a lifecycle visualization above the fold.**

### 2. URL query params as the source of truth for filters

**Reference:** `src/pages/Tasks.tsx:60-86`, `src/pages/WorkflowRuns.tsx:33-37`, `src/pages/Kanban.tsx:29-41`.

`?repo=…&status=…&task=…` lives in the URL on every list page. The back button works. Bookmarks work. Cross-page links can pre-filter (the RepoDetail page's `View Tasks` link in `src/pages/RepoDetail.tsx:155-161`). It's also a prerequisite for sharing operator views in Slack/chat. Bunkhouse's `UI_CONSISTENCY_PLAN.md:22-26` elevated this and the code honored it. Copy it.

### 3. Adaptive refresh — WebSocket-first with polling fallback, and *show* which mode you're in

**Reference:** `src/pages/Tasks.tsx:132-155` + `src/components/ConnectionStatus.tsx:9-34`.

Tasks subscribes to a WebSocket; if the socket drops, a 30s polling loop kicks in. The detail pages use a `refetchInterval` predicate that polls *only while the thing is active* — not while idle (`src/pages/WorkflowRunDetail.tsx:72-76`, `src/pages/TaskDetail.tsx:386-390`). And — critical — a visible *"Connected / Polling every 30s / Last updated 12:04:33"* affordance is rendered in the header (`src/pages/Tasks.tsx:326-337`). Stale data must never masquerade as live data; the dashboard is a system-health surface, and that means the dashboard's *own* freshness is part of the signal. Adopt this verbatim.

### 4. Information-dense compact header on workflow-run detail

**Reference:** `src/pages/WorkflowRunDetail.tsx:226-270`.

Above the step timeline, a single flex-wrap row: progress, duration, step counts as an *inline tally* with colors carrying the meaning (`5 done, 1 running, 1 failed`), repo link, PR link, timestamps. No boxy grid of cards stealing vertical space. This is the right density for a high-frequency operator page. Treadmill workflow-run detail (when we build it) and the task-detail page should both use this header pattern.

### 5. Optimistic updates on inline toggles, with rollback on error

**Reference:** `src/pages/EventTriggers.tsx:232-257` (`toggleMutation.onMutate` snapshots the previous list, mutates the cache locally, rolls back if the server rejects).

Whenever there's an inline toggle or quick action (acknowledge an escalation, snooze, pin, cancel), the click must feel instant. Adopt this React Query mutation pattern verbatim for Treadmill's intervention affordances.

---

## What to explicitly redesign — bunkhouse mistakes we are not inheriting

The bunkhouse dashboard was built page-by-page without re-running consistency afterward; the `UI_CONSISTENCY_PLAN.md` cleanup only landed partially. These are the antipatterns. Treat them as anti-references — see the file, understand what's wrong, do the opposite.

### A. Visual-vocabulary sprawl is the dashboard's biggest disease

**Symptom files (anti-references):**

- `src/components/ui/Badge.tsx:3-41` — eight Badge variants (`status`, `pr_status`, `worker_status`, `pr_watch_status`, `conflict_status`, `event`, plus legacy + cloud-native overlaps *inside* `status` itself).
- `src/pages/TaskDetail.tsx:37-57` — a hand-rolled `getStatusBadgeClasses` Tailwind switch living **in parallel** with `<StatusBadge>`. Header uses local; rest of page uses Badge. Predictable drift.
- `src/components/DataTable.tsx:1-195` vs `src/components/ui/DataTable.tsx:1-108` — two `DataTable` implementations with subtly different APIs. Pages import whichever they happened to grab.
- `src/pages/Roles.tsx:474-753` — three different edit surfaces for the same role: inline `<RoleForm>` panel, `<PreviewModal>` from the row eye-icon, *and* full `/roles/:roleId` detail page. The cleanup plan said "remove inline editing"; it's still there.
- `src/pages/Tasks.tsx:30-40` — status filter shows "Legacy" values (`pending`, `queued`, `in_progress`, `completed`) mixed with current cloud-native values (`registered`, `blocked`, `executing`, `done`) so the operator picks from a vocabulary of double-counted things.

**The redesign rule for Treadmill:**

- **Exactly one `<StateBadge>` component.** It takes a state value from a *closed enumerated vocabulary* and an optional small icon. No per-entity variants. The entity type is communicated by an *icon prefix or container chrome*, not by a separate Badge variant.
- **Exactly one `<DataTable>` component.** It supports: sortable headers, row click, sticky header, virtualization for >200 rows, a "selected row" stripe (we'll want multi-cancel later), pluggable empty state. Delete every hand-rolled `<table>` in the design.
- **One `PageLayout` wrapper for every page.** It owns its own loading, error, and not-found states (with skeleton screens, not "Loading…" text). Detail pages do not reinvent the wrapper, ever. (Bunkhouse had this rule documented at `UI_CONSISTENCY_PLAN.md:13-14`, but `RepoDetail`, `EpicDetail`, and `TaskDetail` all bypass it — see the 16 occurrences of hand-rolled `min-h-screen bg-gray-100 p-8` elsewhere. Don't.)
- **No "legacy" / "transitional" vocabularies.** Treadmill picks the state words once on day one. The API normalizes at its boundary; the UI only ever sees canonical values.

### B. Status colors should signal operator action, not be decorative

**Symptom:** `src/components/ui/Badge.tsx:108-110` + `src/design-tokens.css:148-159` color `failed` and `cancelled` as adjacent reds/grays; Kanban `src/pages/Kanban.tsx:11-19` puts `failed` next to `done`. The operator's mental hierarchy is *"is anything failing that I need to look at?"* — but the visual hierarchy doesn't match it.

**The redesign rule:** color is reserved for operator action.

- **Red — "needs attention right now."** Failed steps, stalled tasks, health-bot escalations, conflicts. Anything where the operator should consider intervening.
- **Amber — "in flight, watch."** Running, pending review, validating. Not a problem, but not done.
- **Green — "good outcome."** Done, merged, validated.
- **Muted gray — "explicit stop / archived."** Cancelled, superseded, abandoned. *Not the same color as failed.* These are decisions, not problems.
- **Neutral chrome — "nothing yet."** Empty states, idle, not started.

And: every red badge in the UI must carry a discoverable *why* — a tooltip with the failing step's name, or a small `?` glyph that links to the failure detail.

### C. Tables should reflect the operator's question, not the data model

**Symptom:** `src/pages/Tasks.tsx:245-323` — the Tasks column order is `Title, Status, Created, PR, Workflow, Repository, ID`. The "Workflow" column renders a workflow UUID, not the workflow name. An operator scanning the page can't answer "which roles are running right now" without clicking in.

**The redesign rule:** every column on the overview must answer a question the operator actually has, ordered by frequency-of-asking. For Treadmill's overview that ordering is something like: *what repo, what task title, where in its life is it (state + which workflow + which role currently), how long has it been there, where's its PR sitting, which account is it billing.* No UUIDs in default columns. Workflow names + the current role's identity as a small inline pill (`planning → coding → review[●]`) — *the multi-step pipeline is the unit of operator attention, so render the pipeline, not the row*.

### D. Detail-page section order should be driven by what's blocking progress

**Symptom:** `src/pages/TaskDetail.tsx:776` — the validation timeline is buried at the bottom of TaskDetail, *after* workflow-run history, live logs, details, dependencies. The lifecycle stepper at the top promises a "Validated" step, but if validation just failed, the operator has to scroll past four other sections to find *what* failed.

**The redesign rule:** detail-page sections are ordered by *"what's blocking this thing's progress right now?"* — not by a static template. If validation is failing, the validation panel comes first under the lifecycle stepper. If CI is failing, the CI failure comes first. If review is pending, the review request comes first. If nothing is blocking, the most-recent-activity section comes first. This is a layout decision the page makes from the data, not a fixed sequence the designer hardcodes.

### E. Inline editing + row click + destructive buttons in the same row = misclicks

**Symptom:** `src/pages/EventTriggers.tsx:322-348` — enable/disable toggle, Edit button, Delete button, *and* row click navigation, all on the same row. Each uses `e.stopPropagation()` and prays. The bunkhouse cleanup plan said "row click does nothing, no detail page" *or* "detail page, no inline toggle" (`UI_CONSISTENCY_PLAN.md:204-211`); implementation diverged.

**The redesign rule:** pick one per page. Either (a) **list with inline config** (no detail page, no row click, inline edit is the affordance) — appropriate for small flat config like event triggers; or (b) **list → detail page** (row click navigates, no inline anything in the row, all edits live on detail) — appropriate for everything with depth. Treadmill defaults to (b) for tasks/plans/runs.

### F. Delete commented-out routes and phantom pages

**Symptom:** `src/App.tsx:25-27, 73-75` (Learnings + LearningDetail commented out, "TODO: Re-enable when backend is ready"); `src/components/Sidebar.tsx:12, 49-50` (matching sidebar entry also commented); `src/pages/Credentials.tsx` (whole page is a `<Navigate to="/settings" />`); `src/pages/Workers.tsx` (261 lines, removed from sidebar per the cleanup plan, still in the codebase); `src/pages/Volumes.tsx` (505 orphaned lines); `src/pages/TaskDetail.tsx:490, 494, 653` (vestigial `TabType` scaffolding with one tab and an unused setter).

**The redesign rule:** Treadmill ships nothing commented out. Features the backend isn't ready for are feature-flagged off or not in the design at all. The phantom routes are a maintenance liability *and* a navigation signal to operators that this part of the system is unfinished. Don't.

### G. Forms with six collapsibles deep are configuration mazes

**Symptom:** `src/pages/Roles.tsx:251-454` — six independent expand/collapse states for one role create form (responsibilities, boundaries, handoff, skills, hooks, base-profile-preview). A new operator faces a tall accordion with nothing pre-expanded except responsibilities.

**The redesign rule (when we eventually build role/repo configuration in Treadmill):** two-column layout — left column is identity + required fields, right column is a live-rendered preview of the result. Optional/advanced fields are a flat checklist below, not nested collapsibles. *Not in v1 scope*, but if Claude Design generates a config-page mock as a "showcase," apply this rule.

---

## What's new in Treadmill that doesn't exist in bunkhouse

These surfaces have no bunkhouse precedent. Design them fresh, consistent with everything above.

### 1. Per-account billing strip

Treadmill routes Claude calls to different accounts per repo (ADR-0055). The overview header carries a small strip showing each named account's last-24h spend (tokens + USD estimate). It is small — a single row, not a section. Clicking an account *might* later route to a spend detail page; in v1 it's static. Each repo row in the overview table carries a tiny account pill in the trailing column.

### 2. Mode-aware repo badge

Each repo is either `conform` (Treadmill commits its scaffolding) or `adapt` (repo stays pristine, docs in external store) (ADR-0050). The overview table's repo cell carries a tiny mode pill next to the repo name. Adapt-mode rows have a subtle distinct treatment (a one-pixel inset border on the repo cell, or a small `↗` glyph) so the operator can scan for them.

### 3. Health-bot escalation banner

Treadmill has scheduled health bots that scan for stuck tasks, stale runs, etc., and emit `task.escalated_to_operator` events when they find something the operator should look at (ADR-0035). The overview must carry an escalation banner at the very top — above the table — listing any active (unacknowledged) escalations from the last 24h. One line per escalation, linking to the task. Acknowledging an escalation removes it from the banner and persists to the events table. **This is the closest thing to a notification system Treadmill has; it must be visible without scrolling.**

### 4. Multi-level hierarchy: plan → task → workflow run → step → PR

Bunkhouse's UI is mostly flat (tasks, runs, workflows as parallel lists). Treadmill has actual depth. The hierarchy is:

- **Plan** — a markdown doc that spawns a set of tasks.
- **Task** — one logical change, eventually one PR.
- **Workflow run** — a verb applied to a task at a moment (wf-quick, wf-review, wf-ci-fix, wf-feedback, wf-conflict, wf-validate). A task has many runs over its life.
- **Step** — one role's invocation within a run.
- **PR** — the artifact on GitHub.

Drill-in pattern recommendation: **two levels inline, deeper levels link out.** So on `/tasks/:id`, runs are inline-listed with their steps inline-expandable; clicking a step navigates to its detail page (logs, full output, errors, token usage). This is the trade-off — totally-inline gets unwieldy at 5 levels, totally-link-out is 4 clicks to investigate one PR. Two-and-three is the right split.

---

## The two pages this design exploration must produce

**Don't design every page in Claude Design.** The two pages where visual polish matters most are below. Everything else gets lifted directly from bunkhouse's patterns (DataTable, FilterBar, PageLayout) when we wire it up.

### Page 1: Overview (`/`)

What it must show, in roughly this top-to-bottom order:

1. **Connection / freshness affordance** in the page header — same as bunkhouse `Tasks.tsx:326-337`.
2. **Per-account spend strip** — one row, named accounts, last 24h tokens + USD est.
3. **Worker fleet + heartbeat row** — running worker count, autoscaler "alive since" timestamp, scheduler last-tick timestamp. Each is a small chip; red if stale.
4. **Health-bot escalation banner** — if any active, one line per. If none, the banner is absent (don't render an empty "no escalations" state — silence is the signal).
5. **Non-terminal tasks table** — the main surface. Sorted by age descending (oldest stale stuff at top, because that's what needs operator attention). See the data-shape spec in `docs/plans/2026-05-26-treadmill-dashboard-v1.md` §"Data shapes — paste this section into Claude Design" for the exact fields.
6. **Recent events feed** — side rail or below the table, last ~20 system events with task-id links.

### Page 2: Task detail (`/tasks/:id`)

What it must show, in roughly this top-to-bottom order — but remember rule **D**: section order is driven by what's blocking progress, not this fixed sequence. Treat this as the "everything's fine" default ordering.

1. **Lifecycle stepper** — full width, ~5 nodes, current highlighted, failed shown red with X glyph.
2. **Compact info bar** — title, repo (link to GitHub), plan link, account routing badge, created/started/completed timestamps.
3. **PR strip** (if the task has a PR) — pr_number link, branch, head_sha (short), CI conclusion, review decision, validate decision, conflict status, derived mergeability as the single source of truth chip.
4. **Action affordances** — Cancel (with confirmation modal asking for a reason), Open PR on GitHub (deeplink), Retry last failed step (only if applicable), Override review (only if applicable), Acknowledge escalation (only if applicable). Buttons grouped, destructive actions visually distinguished.
5. **Workflow runs timeline** — inline list, each run expandable to show its steps inline; clicking a step navigates to its detail page.
6. **Events feed filtered to this task** — at the bottom, smaller than overview's feed.

If anything is blocking progress (failed validation, conflict, CI failure), promote the relevant section above the workflow-runs timeline.

---

## Mandatory design rules — non-negotiable

These are the rules that prevent the bunkhouse antipatterns from recurring.

1. **One `<StateBadge>` component** with a closed enumerated vocabulary. No per-entity variants. The entity type is communicated by an icon prefix or container chrome.
2. **One `<DataTable>` component.** Delete every hand-rolled `<table>`.
3. **One `<PageLayout>`** that owns its own loading / error / not-found states. Skeleton screens, not "Loading…" text. No page wrapper anywhere else.
4. **Every detail page has a lifecycle visualization above the fold.**
5. **Detail-page section order is driven by what's blocking progress.** Not a template.
6. **Red is reserved for "needs attention now."** Cancelled / superseded / archived states are muted gray, not red.
7. **Every red badge has a discoverable *why*** — tooltip or `?` link to the failure detail.
8. **Connection / freshness affordance visible on every live page** — operator must always know if they're seeing live or stale data.
9. **One affordance class per row** — either inline edit (no detail page) or detail page (no inline edit), never both.
10. **No commented-out routes, no phantom pages, no vestigial state.** Feature flags or nothing.

---

## How this gets handed back

When the canvas is ready, use Claude Design's **Send to Claude Code** flow. The bundle should include: the component tree, the design tokens (colors / spacing / type that descend from bunkhouse's `design-tokens.css`), the layout intent, and any referenced assets. I'll pick up the bundle, lift the bunkhouse scaffolding (Vite + React 19 + TS + Tailwind + TanStack Query + `WebSocketContext` from `bunkhouse/services/dashboard/src/contexts/WebSocketContext.tsx`), reshape the visual layer to match the bundle, and wire the live data + actions against the Treadmill API. The data shapes I'll wire against are in `docs/plans/2026-05-26-treadmill-dashboard-v1.md` under §"Data shapes."

If the bundle's mocked fields don't match the data spec, I'll flag the drift before any code lands — better to iterate visually one more round than ship a UI that needs structural rework.
