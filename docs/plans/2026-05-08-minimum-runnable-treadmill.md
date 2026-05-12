# Plan: Minimum runnable Treadmill

- **Status:** active
- **Date:** 2026-05-08
- **Related ADRs:** ADR-0001, ADR-0002, ADR-0004, ADR-0006, ADR-0007, ADR-0009, ADR-0010, ADR-0011

## Goal

Build the smallest Treadmill that can ingest a plan (pre-authored or planned-from-intent), dispatch tasks, run authoring workers in containers, and produce PRs end-to-end against a Treadmill-managed repo, locally on the spike substrate. Phase 2 closes when an end-to-end run completes for both intake scenarios from ADR-0010 and every workflow we ship has fired at least once with observable output.

## Success criteria

By the time Phase 2 closes, every one of the following must be observably true on a developer machine via `treadmill local up`:

1. **Scenario 1 (pre-authored plan):** `treadmill plan submit --doc <path>` creates a `Plan` row with `status=active`, parses tasks from the doc's `## sequence_of_work` YAML, and creates `Task` rows. Listing via `treadmill plan show <id>` reflects the spawned tasks.
2. **Scenario 2 (intent-only):** `POST /api/v1/plans { intent }` creates `Plan(status=drafting)`, `wf-plan` dispatches a planning worker, the worker authors `docs/plans/<date>-<slug>.md` on `plan/<plan-id>-<slug>`, opens a PR, and on PR merge the plan flips to `active` and tasks spawn.
3. **Implicit one-task plan:** `treadmill submit "<intent>"` creates a `Plan(status=active, doc_path=null)` with one `Task` referencing `wf-author`.
4. **End-to-end task execution:** an authoring worker picks up a task, branches `task/<short-id>-<slug>`, authors the change, pushes, opens a PR. PR title and branch follow ADR-0010 conventions.
5. **Every starter workflow fires at least once with logged output:** `wf-plan`, `wf-author`, `wf-review`, `wf-validate`, `wf-feedback`, `wf-ci-fix`, `wf-conflict`. Their outputs (review comments, validation reports, fix attempts) appear in worker logs and are persisted as workflow-run-step output.
6. **All Phase 2 production code ships with tests** per `rule:features-ship-with-tests`.
7. **`cdk synth` passes** for the updated stack (API service + worker service + supporting resources). The same stack is real-AWS-deployable per ADR-0002 criterion 6.
8. **Treadmill submits its own first PR** — a Treadmill task that modifies Treadmill itself, authored by a Treadmill worker, lands as a PR. (This is the gate to Phase 3, not strictly Phase 2's close, but it lives at the boundary.)

## Constraints / scope

### In scope

- **Treadmill API** (FastAPI, Python ≥3.12): plans, tasks, workflows, roles, skills, hooks, event-triggers, webhooks (per ADR-0007), workers, runs, steps. Postgres for state; Redis for dependency-resolution and pending-event buffering; SNS+SQS via the spike's CDK substrate.
- **Worker** image (one base, single tier `standard`): replaces the noop worker. Reads role config dynamically. Runs Claude Code (or equivalent) in the container. Authors PRs via `gh` CLI. Cribbed in shape from bunkhouse but rewritten — no code lift.
- **CLI** (`treadmill`): `plan submit`, `submit`, `plan show`, `plan list`, `task show`, `task list`, `status`, `logs`. Talks to the API over HTTP.
- **Strict YAML task schema** (see below) — `## sequence_of_work` block in plan docs, parsed deterministically.
- **Seven starter workflows** with their roles:
  - `wf-plan` (role-planner): research + plan-author for Scenario 2.
  - `wf-author` (role-author): the worker that produces a PR for one task.
  - `wf-review` (role-reviewer): code-quality review on `pr_opened`.
  - `wf-validate` (role-validator): **task-completion** check on `pr_opened`. Runs the task's declared `validation` entries (deterministic stubs in Phase 2; real LLM-judge in Phase 4 alongside the rule engine).
  - `wf-feedback` (role-feedback): addresses review comments on `pr_review_submitted`.
  - `wf-ci-fix` (role-ci-fixer): fixes failing CI on `check_run_completed` failed (capped retries).
  - `wf-conflict` (role-conflict-resolver): resolves merge conflicts when detected.
- **Event triggers** for the auto-firing workflows above.
- **Plan-doc parser** that extracts strict-YAML tasks; rejects loose / freeform sequences.

### Out of scope

- Real GitHub webhook ingestion (the receiver endpoint exists per ADR-0007's contract; live webhook delivery is Phase 4).
- Pre-prod environments per changeset (Phase 4 — ADR-0007 implementation).
- Rule engine (Phase 4). `wf-validate` ships as a stub that records intent; real evaluation arrives with the engine.
- Auto-promotion of learnings / rules (Phase 4).
- Multiple compute tiers; multi-image worker stack.
- Authentication / authorization beyond a shared API key. Multi-user is out of scope until evidence demands it.
- Dashboard UI. Inspection is via CLI + `bunk`-style verbs.
- Cross-repo cascade. Single managed project (Treadmill itself) until Phase 5.

### Budget

Four working weeks. If end of week 4 does not show success criteria 1–7 passing, we run a post-mortem and split Phase 2 into Phase 2A (API + CLI) and Phase 2B (workers + workflows). The criterion-8 demonstration (Treadmill's first self-authored PR) may slip into a Phase 2-to-3 transition week.

## Strict YAML schema for tasks

The `## sequence_of_work` block in a plan doc is a YAML list. Each entry conforms to this schema. The plan-doc parser rejects entries that violate it.

```yaml
sequence_of_work:
  - id: t0                              # required; unique within plan; kebab-case
    title: "Add users table migration"  # required; terse, imperative
    workflow: wf-author                 # required; must reference a known workflow
    depends_on: []                      # optional; expressions per ADR-0007 (task.<id>.<event>)
    intent: |                           # required; what the change is and why
      Add Alembic migration for the users table with id (UUID PK),
      email (unique, NOT NULL), created_at (timestamptz, default now()).
    scope:
      files:                            # required; at least one entry; relative paths
        - services/api/alembic/versions/<auto>.py
        - services/api/src/models/user.py
      services_affected:                # optional; derived from files if omitted
        - api
      out_of_scope:                     # optional; what NOT to touch — guards scope creep
        - any other migration files
        - anything outside services/api/
    validation:                         # required; at least one entry
      - kind: deterministic              # or: llm-judge
        description: "alembic upgrade head runs cleanly against a fresh database"
        # script field added in Phase 4 when the rule engine wires execution
      - kind: llm-judge
        description: |
          The migration creates a users table with the columns
          described in `intent`, and there is no other schema change.
```

Required fields: `id`, `title`, `workflow`, `intent`, `scope.files`, `validation` (at least one entry). Other fields optional but encouraged.

### `--dev` flag for local-only fast paths

`treadmill submit "<intent>" --dev` and `treadmill plan submit --doc <path> --dev` skip the `wf-plan` PR-merge gate and create a `Plan(status=active)` directly. The flag is **honored only when the API detects it is running locally**, gated by the `AWS_ENDPOINT_URL` env var pointing at the moto endpoint (or an explicit `TREADMILL_LOCAL=true`). When the API is running in a real AWS account, the flag is silently ignored and the standard path runs. Tests cover both code paths.

## Sequence of work

### Week 1 — API foundation

- Day 1: New service at `services/api/`. uv project; FastAPI; alembic; Postgres connection via env-driven URL; Redis client; tests scaffolded. **Lands with:** healthcheck endpoint + a passing test that hits the live API in the spike substrate.
- Day 2: Plan + Task models; alembic migrations for `plans`, `tasks`, `workflow_versions`, `workflow_runs`, `workflow_run_steps`, `roles`, `skills`, `hooks`, `event_triggers`, `task_prs`, `events`, `task_dependencies`, `task_status` VIEW. The `task_status` VIEW is cribbed from bunkhouse migration 020 (five priority categories: cancelled > blocked > registered > executing/failed > pr_state/done; workflow-id prefix on active states; PR-aware overlay for failures). Pydantic event-type models authored alongside the `events` table. Tests cover migrations up + down + a fixture-driven test of the VIEW that asserts each priority category resolves correctly.
- Day 3: Plans router (`POST /plans`, `GET /plans/{id}`, `GET /plans/{id}/tasks`, `POST /plans/{id}/submit-doc`). Plan-doc parser as a separate module (testable in isolation against fixture docs). Tests exercise both Scenario-1 (`--doc`) and intent-only paths.
- Day 4: Tasks router; Roles, Workflows, Skills, Hooks routers (CRUD-ish, mostly read for v0); EventTriggers router. Tests cover basic round-trips.
- Day 5: Webhooks router with HMAC-SHA256 verification per ADR-0007 (events persisted + published; receiver wired but no GitHub delivery yet). Tests cover signature verification (good, bad, missing) and event normalization.

### Week 2 — Worker + CLI

- Day 1: Worker image rewrite. Replace the noop worker. Worker fetches role config from API. Runs Claude Code in the container with the role's `system_prompt`. Tests cover env wiring and one round-trip against a fixture role.
- Day 2: PR creation via `gh` CLI. Branch naming per ADR-0010. Worker reports back to API on completion. Tests cover branch-naming determinism and the report-back call.
- Day 3: CLI scaffolding (`treadmill` — typer-based). `plan submit`, `plan show`, `plan list` commands.
- Day 4: CLI completes — `submit` (intent-only), `task show`, `task list`, `status`, `logs`. Smoke test: end-to-end `treadmill submit "<intent>"` → Plan + Task appear in the API.
- Day 5: Worker + CLI end-to-end smoke. A task with a tiny diff (e.g. add a comment) goes from CLI → API → SQS → worker → PR. **Gate check for week 2.**

### Week 3 — Workflow plumbing

- Day 1: `wf-plan` + `role-planner`. Planning worker reads existing ADRs, drafts a plan doc + diagrams (ADR-0004-conformant), opens PR. Tested against a fixture intent.
- Day 2: `wf-author` + `role-author` (refines week-2 work into a real role with prompt + skills + hooks). Tests cover role-config fetch and prompt composition.
- Day 3: `wf-review` + `role-reviewer`. Auto-fires on `pr_opened` event. Reviewer reads diff, posts a review comment. Tests cover trigger evaluation and comment posting.
- Day 4: `wf-validate` + `role-validator`. Reads task's declared `validation` entries; for each entry, records its stated description as a check (executable in Phase 4). Posts a status comment summarizing validation outcomes. Tests cover schema parsing and comment formatting.
- Day 5: Event-trigger registry seeded with the auto-firing workflows. End-to-end test: PR opens → wf-review and wf-validate both fire and comment. **Gate check for week 3.**

### Week 4 — Integration + dogfooding

- Day 1: `wf-feedback` + `role-feedback` on `pr_review_submitted`. Worker addresses review comments by pushing to the existing branch.
- Day 2: `wf-ci-fix` + `role-ci-fixer` on `check_run_completed` failed. Capped retry (3 attempts default).
- Day 3: `wf-conflict` + `role-conflict-resolver` on conflict detection. Worker resolves and pushes.
- Day 4: End-to-end happy-path test against a sample plan with three tasks. All seven workflows fire. Logs / worker outputs / persisted artifacts inspected.
- Day 5: **Phase 3 boundary** — Treadmill submits its own first PR (a tiny, low-risk change to itself). Tests pass. Plan transitions to `completed`. Post-mortem authored.

## Diagram

```mermaid
sequenceDiagram
    actor Human
    participant CLI as treadmill CLI
    participant API as Treadmill API
    participant Bus as Event bus (SNS+SQS)
    participant Planner as wf-plan worker
    participant Author as wf-author worker
    participant Reviewer as wf-review worker
    participant Validator as wf-validate worker
    participant Repo as Repo (main)

    alt Scenario 1 (pre-authored plan on main)
        Human->>CLI: treadmill plan submit --doc <path>
        CLI->>API: POST /plans (doc_path)
        API->>API: parse YAML; Plan(status=active); spawn Tasks
    else Scenario 2 (intent only)
        Human->>API: POST /plans (intent)
        API->>API: Plan(status=drafting)
        API->>Bus: dispatch wf-plan
        Bus-->>Planner: step.ready
        Planner->>Repo: branch plan/<plan-id>-<slug>; commit doc
        Planner->>Repo: open PR
        Human->>Repo: review + merge
        Repo-->>API: pr_merged (webhook stub)
        API->>API: Plan(status=active); spawn Tasks
    end

    loop for each Task in Plan
        API->>Bus: dispatch wf-author
        Bus-->>Author: step.ready
        Author->>Repo: branch task/<short-id>-<slug>
        Author->>Repo: author + push + open PR
        Repo-->>API: pr_opened (webhook stub)
        par
            API->>Bus: dispatch wf-review
            Bus-->>Reviewer: step.ready
            Reviewer->>Repo: read diff; post review comment
        and
            API->>Bus: dispatch wf-validate
            Bus-->>Validator: step.ready
            Validator->>Repo: read task validation; post status comment
        end
        Human->>Repo: review + merge
    end
```

## Risks / unknowns

- **`wf-plan` prompt design is the heaviest agent-judgment call we ship.** Mitigation: budget a half-day specifically for prompt iteration on day 1 of week 3; if it slips, we ship Scenario-2 with a more constrained planner that produces a plan-doc shell for human completion.
- **Plan-doc YAML parser brittleness.** Mitigation: strict schema with explicit error messages; reject ambiguous docs rather than guess.
- **Worker isolation in containers.** Tests are slow if every test spins a real container; we mock the worker shell for unit tests and run a single integration test that exercises a real container per week.
- **Treadmill's own CDK stack growth.** Adding API + worker services materially expands the spike's stack. Mitigation: incremental adds; CDK assertions land alongside each new construct.
- **Event-trigger evaluation race conditions.** Cribbed cache-then-heal pattern from bunkhouse mitigates, but the implementation is non-trivial. Mitigation: integration test suite exercises ordering-sensitive scenarios.
- **wf-validate is a stub through Phase 2.** Real validation arrives in Phase 4. Mitigation: ensure the stub records *what* would have been validated (the task's declared criteria) so Phase 4 has a clean upgrade path.
- **`task_status` VIEW is hard to evolve once shipped.** Bunkhouse evolved it across three migrations; we adopt the post-evolution form, but new lifecycle events that drive new statuses will require migration. Mitigation: a fixture-driven test suite that exercises every priority category lets us catch regressions on every migration.
- **Pydantic + JSONB discipline must hold.** A single `dict[str, Any]` access bypasses the contract. Mitigation: a lint rule (eventually) and reviewer convention (immediately) forbids raw `payload[...]` access outside the per-type model layer.

## Status correction (2026-05-11)

The 2026-05-08 Week-2-closed entry below was over-stated. A 2026-05-11 adversarial review (manual; auto-review wiring is unbuilt) surfaced ~30 findings across eight buckets — including an ADR-0010 branch-format violation, a missing `task_prs` writer that breaks the webhook→trigger chain, the worker→consumer Pydantic-boundary contract honored only on one side, and a dry-run smoke being passed off as Phase 2 success criterion 4 verification. The user rejected closing Week 2 with these open: *"I don't think that we can close week 2 until all of this is resolved. We're at the very beginning of a large architectural shift. If there's a time to be pedantic it's right now."* Captured at `docs/learnings/2026-05-11-review-driven-phase-closure.md`. The closure work is sequenced at `docs/plans/2026-05-11-week-2-closure.md`. Week 2 is **re-opened** until that closure plan transitions to `completed`. The original Week-2-closed entry below is preserved as historical record.

## Decisions captured during execution

- **2026-05-08** Day 1A landed: `services/api/` scaffolded as a uv workspace member with FastAPI + asyncpg + redis-py + SQLAlchemy + alembic + boto3 dependencies wired in `pyproject.toml`. Module layout: `treadmill_api/{__init__.py, app.py, config.py, health.py, cli.py}`. Pydantic-settings `Settings` class loads env-driven config (`TREADMILL_*`, `DATABASE_URL`, `REDIS_URL`, `AWS_ENDPOINT_URL`). FastAPI app factory pattern (`create_app()`) keeps tests isolated. Healthcheck router exposes `/health` (liveness) and `/health/ready` (readiness — empty-checks shell awaiting Day 1B's dependency probes). Three unit tests, all green; full workspace test suite at 43 passing.
- **2026-05-08** Local-run verification (manual): `TREADMILL_PORT=8842 uv run treadmill-api` boots uvicorn cleanly; `curl /health` and `curl /health/ready` return the expected JSON; `/docs` and `/openapi.json` serve correctly. Default port changed from 8080 → 8088 because 8080 was squatted on the dev machine; the local adapter will set `TREADMILL_PORT` explicitly per deploy.
- **2026-05-08** Day 1A's "completion" overclaimed against the plan's literal Day 1 gate. The plan calls for "a passing test that hits the live API in the spike substrate" — Day 1A delivered TestClient unit tests only, not a real-uvicorn-against-real-substrate integration test. That gate is satisfied by Day 1D's deliverable, not Day 1A. Captured as `learning: 2026-05-08-test-client-is-not-running`. Day 1's full gate remains pending until Day 1D ships.
- **2026-05-08** Day 1B landed: async SQLAlchemy engine factory (`treadmill_api.database`), async Redis client factory (`treadmill_api.cache`), and a `DependencyProbe` protocol with `PostgresProbe` + `RedisProbe` implementations (`treadmill_api.dependencies`). Both clients return `None` when their URL is unset so the API boots without a database. Alembic is wired (`alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`) — env.py pulls the URL from `Settings` at runtime and rewrites `+asyncpg` → `+psycopg` for sync execution. FastAPI lifespan handler creates engine + redis at startup, registers probes on `app.state.probes`, disposes at shutdown. `/health/ready` now runs the probes and returns 503 if any is `unreachable`; `not_configured` does not flip overall status. 18 new tests + 3 existing health tests, all green; full workspace suite at 61 passing. Manual local boot verified again: probes correctly report `not_configured` for postgres + redis when no env vars are set. **Found by tests**: pydantic-settings with field aliases needs `populate_by_name=True` in `SettingsConfigDict` to accept Python field names as constructor kwargs — fixed.
- **2026-05-08** Day 1C landed: spike CDK stack now declares Postgres (FargateTaskDefinition + Service, desired=1, port 5432), Redis (FargateTaskDefinition + Service, desired=1, port 6379), and Treadmill API (FargateTaskDefinition + Service, desired=1, port 8088, env including `DATABASE_URL` + `REDIS_URL` + `TREADMILL_LOCAL=true`). API Dockerfile (`services/api/Dockerfile`) builds `treadmill-api:dev`. The local adapter learned about non-autoscaled "services": `runner.py` adds `ServiceSpec` + `resolve_services` + `autoscaled_service_logical_ids`; `runtime.py` adds `_start_services`, refactors `_run_container` to accept name + role + port_mappings, adds `_ensure_image` (which auto-pulls public images and refuses to pull `:dev`/`:local` tags). Default host-port overrides for common dev ports (5432→15432, 6379→16379) avoid clashes with local installs. CDK assertion tests for the new task defs + services + env vars + port mappings. Full test counts: services/api 21, tools/local-adapter 38, infra 13 = **72 unit tests passing**.
- **2026-05-08** Day 1D closed the literal Day 1 gate: integration tests at `services/api/tests/test_integration_local.py`, env-var-gated via `TREADMILL_INTEGRATION=1`, exercise the live API in the substrate via real HTTP calls. Five tests pass: liveness; postgres probe reaches `treadmill-postgres:5432` via container DNS; redis probe reaches `treadmill-redis:6379`; overall readiness; OpenAPI doc served. End-to-end manually verified: `treadmill-local up` brings up moto + postgres + redis + API + autoscaler, `curl localhost:8088/health/ready` returns `ok` with both deps reachable, `treadmill-local down` is clean. **Day 1 of the Phase 2 plan is now satisfied.**
- **2026-05-08** Day 2A landed: SQLAlchemy models in `treadmill_api/models/` across five files (plan.py, task.py, workflow.py, run.py, event.py) covering all 16 entities from ADR-0010 + ADR-0007 (Plan, Task, TaskPR, TaskDependency, Workflow, WorkflowVersion, WorkflowVersionStep, WorkflowRun, WorkflowRunStep, Role, Skill, Hook, RoleSkill, RoleHook, EventTrigger, Event). UUID PKs with `gen_random_uuid()` server defaults; TIMESTAMPTZ everywhere; JSONB only on `events.payload` and `workflow_run_steps.output` per ADR-0011 (a `test_no_unexpected_jsonb_columns` test enforces the rule). First alembic migration `0001_create_core_tables.py` autogenerated against live Postgres; upgrade + downgrade round-trip verified. 9 unit tests confirm model importability + schema shape; 6 integration tests (TREADMILL_INTEGRATION=1) confirm migration applies cleanly, all 17 expected tables exist (16 + alembic_version), task_prs composite PK is correct, events.payload is JSONB, plan inserts round-trip end-to-end.
- **2026-05-08** Day 2B landed: `treadmill_api/events/` package with 17 typed event payload classes across base.py, task.py, plan.py, step.py, github.py, plus a registry.py mapping `(entity_type, action) → cls`. Pydantic strict-mode (`extra="forbid"`) — unknown fields raise `ValidationError`. `parse_payload()` and `encode_payload()` are the only seam between JSONB storage and typed application code per ADR-0011. 28 unit tests cover round-trips for every event type, malformed-payload rejection, registry coverage of all Phase-2-minimum event types, and an `AuthorStepOutput` type for `wf-author` step outputs. ADR-0011's "no `dict[str, Any]` access" claim now has a concrete enforcement layer.
- **2026-05-08** Day 2C landed: second alembic migration `0002_task_status_view.py` creates the `task_status` Postgres VIEW cribbed from bunkhouse migration 020. Five priority categories (cancelled > blocked > registered > `<wf>: executing` > pr_state / done), workflow-id prefix on active states (`wf-author: executing`), PR-aware overlay for failures (`pr_merged (wf-review: failed)`). Adjustments from bunkhouse: Treadmill's `tasks.plan_id` (not `epic_id`); LATERAL JOIN through `workflow_versions` to expose the workflow slug since Treadmill workflow_runs reference a version not a slug; `entity_type='github'` for PR events (not bunkhouse's normalized `task` entity). Dependency evaluation supports `task.<id>.pr_merged`, `task.<id>.run.completed`, `task.<id>.step.<name>.completed`; deployment expressions are forward-compat-FALSE until ADR-0007 ships its full implementation. **13 fixture-driven integration tests** assert every priority category resolves correctly: cancelled > others, blocked, registered, executing (running + pending steps), failed-no-PR, pr_opened with overlay, pr_merged with overlay, pr_opened, pr_merged, review_passed, done. Test isolation via `TRUNCATE … RESTART IDENTITY CASCADE`. **Day 2 of the Phase 2 plan is now satisfied.** Test totals: services/api 58 unit + 24 integration = 82; tools/local-adapter 38; infra 13. **133 tests passing.**
- **2026-05-08** Day 3 landed: plan-doc parser at `treadmill_api/parsers/plan_doc.py` extracts the `## sequence_of_work` YAML from markdown, parses it, and validates strictly against `TaskSpec` / `TaskScope` / `TaskValidationCheck` Pydantic models. 17 unit tests cover heading + fence extraction (yaml + yml fences), schema validation (required fields, empty lists, unknown kinds, extra fields), and unique-task-id enforcement. Plans router at `treadmill_api/routers/plans.py` exposes four endpoints — `POST /api/v1/plans` (both Scenario 1 with `doc_content` and Scenario 2 with `intent` only), `GET /plans/{id}`, `GET /plans/{id}/tasks` (LEFT-JOINs the `task_status` VIEW so `derived_status` arrives alongside the task fields), `POST /plans/{id}/submit-doc` (Scenario 2 follow-up; idempotent — second call returns 409). 12 integration tests pass against the live API, covering both intake paths, both error modes (PlanDocFormatError + Pydantic ValidationError), 404 / 422 for missing or invalid IDs, derived_status returning `registered` for newly-spawned tasks. Found in testing: had to catch `pydantic.ValidationError` alongside `PlanDocFormatError` in the router (initial 500 surfaced as 400 after the fix). Total tests: services/api 75 unit + 36 integration = 111. **Workspace total: 162 passing.**
- **2026-05-08** Day 4 landed: six new routers — Skills, Hooks, Roles, Workflows + WorkflowVersions (nested), Tasks, EventTriggers. Each does POST + GET (single) + GET (list); no PATCH/DELETE at v0 per the plan's "mostly read" framing. Cross-cutting validations: Roles router 400s on unknown skill/hook slugs; Workflows router 400s on unknown role slugs; EventTriggers honors the (repo, event_type) UNIQUE constraint with 409. Tasks router LEFT-JOINs `task_status` for `derived_status`; supports list filters by repo/plan_id/derived_status. **26 new integration tests** organized by router class — all green. **Workspace total: 188 passing** (services/api 75 unit + 62 integration = 137; tools/local-adapter 38; infra 13).
- **2026-05-08** Day 5 landed: webhooks router at `POST /api/v1/webhooks/github` per ADR-0007. Three pure modules — `webhooks/signatures.py` (HMAC-SHA256 with `hmac.compare_digest` and empty-secret dev short-circuit), `webhooks/normalize.py` (GitHub event + action → internal verb + payload, with the four Phase-2 mappings), and the receiver router (verify → parse → normalize → validate via the Pydantic event registry → look up task_id via task_prs case-insensitively → insert Event row → publish). 22 unit tests + 9 integration tests. `GITHUB_WEBHOOK_SECRET` is a new env var; unset = dev short-circuit.
- **2026-05-08** Day 5+ landed: replaced the eventbus stub with a real SNS-backed publisher and shipped cache-then-heal pending-events buffering. **Day 5 / Week 1 of the Phase 2 plan is now satisfied.**
- **2026-05-08** Week 2 partial: shipped the **`treadmill` CLI** as a new workspace member at `cli/`. Commands: `plan submit` (--doc / --intent), `plan show`, `submit` (intent shorthand creating implicit one-task Plan + Task per ADR-0010), `task show`, `task list` (filterable by repo / plan / derived_status), `status` (live + readiness check). Argument parsing via Typer; HTTP via httpx; presentation via Rich tables. v0 reads `TREADMILL_API_URL` (default `http://localhost:8088`) and `TREADMILL_API_KEY` (reserved; unused at v0) from env. **15 unit tests** (pytest-httpx mocking) + **4 integration tests** against the live API. **Workspace total: 245 passing** (cli 15 unit + 4 integration = 19; services/api 97 unit + 76 integration = 173; tools/local-adapter 38; infra 15). `plan list` is stubbed pending a list-plans API endpoint. **Worker rewrite (Week 2 Day 1–2) is paused pending four design decisions surfaced in chat.**
- **2026-05-08** Week 2 — event-driven coordination + dispatch landed (the prerequisites for the worker rewrite). Decisions confirmed: (1) **Claude Code CLI** in the worker (not the Anthropic SDK), leveraging the user's Claude subscription via the OAuth credentials file mounted into the container; model selectable at the role layer; (2) **real GitHub** for production with hermetic local-bare-repo mode for dev/test; (3) **auto-dispatch on task create** when the parent Plan is active; (4) **fully event-driven** coordination — workers report via SNS, no synchronous HTTP back-channel. Shipped: CDK adds an SQS coordination queue subscribed to the events SNS topic with raw delivery and grants the API task role consume + work-queue send permissions; the local adapter rewrites moto's host-visible `localhost` URLs to the in-network hostname so containers reach moto correctly. New `treadmill_api/coordination/` consumer long-polls the events queue and projects step lifecycle events onto `workflow_run_steps.status` (the single mutable column per ADR-0011); started/stopped via the lifespan handler. New `treadmill_api/dispatch.py` materializes a WorkflowRun + WorkflowRunSteps when a Task is created under an active Plan, persists a `step.ready` Event row, publishes the typed event to the events SNS topic, and sends a thin `{step_id}` claim to the FIFO work queue (MessageGroupId=run_id) — that claim is what the autoscaler watches to spin up workers.
- **2026-05-11** Week 2 honestly closed via the closure plan at `docs/plans/2026-05-11-week-2-closure.md`. The 2026-05-08 "Week 2 closed" entry below is preserved as historical record; the over-claim it described is corrected by what shipped across this closure's four phases. **Phase 1 (foundation, four parallel agents)** landed the easy honesty wins: ADR-0010 branch format `task/<short-id>-<slug>` with a slugifier + `EXIT_AFTER_STEP=true` restored on both worker config and CDK agent env per ADR-0002 + credentials mount RW + pinned `@anthropic-ai/claude-code@1.0.110` in the agent Dockerfile + `gh` mode removed from `git.py` with `REPO_MODE=github` raising explicitly + `compute_tier` ripped from the wire (column kept in DB as forward-compat ballast) + `WorkTopic` SNS topic deleted + `assert` → explicit `WorkerContextError` raises + shared event-schema dep via uv workspace source + ADR-0008 Stop hook with AGENTS.md session-end paragraph + real-binary Claude CLI flag smoke gated on `TREADMILL_CLAUDE_BINARY_SMOKE=1`. **Phase 2 (Pydantic + lifecycle events + dep persistence + plan VIEW)** brought ADR-0011's "Pydantic at every boundary" to actual symmetry: worker `_publish` validates via typed payload classes; `StepCompleted.output` promoted to typed `AuthorStepOutput | dict` union; coordination consumer validates payloads through the registry before projecting (per decision #2, on `AuthorStepOutput` validation failure it logs + writes raw dict + marks completed — the source-of-truth Event row is the authority). Dispatcher emits `PlanRegistered` / `PlanActivated` / `TaskRegistered` lifecycle events; `Dispatcher.from_app_state` enables no-Request callers; publisher wraps boto3 errors as typed `PublishError`. Worker stops accepting empty diffs: `--allow-empty` dropped, `has_staged_changes` gates commit, no-author publishes `step.failed`. Redelivery-safe checkout via fetch + `checkout -B` and `--force-with-lease` on push. `step.started` now published before fetching context, with the dispatcher's claim body carrying `step_id`, `task_id`, `plan_id`, `run_id`. `CoordinationProbe` registered on `/health/ready`. `task_dependencies` rows now persisted with grammar validation; `task_validations` side table + alembic migration 0003 + persistence; `plan_status` VIEW + alembic migration 0004 + `derived_status` on plan responses, mirroring the `task_status` pattern from Week 1; starter workflow seed module + `treadmill workflows seed-starters` CLI command. **Phase 3 (failure replay + dependency gates + harness)** closed the dispatch-failure path and the dependency-gated dispatch path: `dispatch_publish_failed` Event-row marker on bus/queue failure + replay loop with 30s tick that re-publishes from the marker; consumer poll-loop wraps exponential backoff (`1, 2, 4, 8, 16, 30`) with `_health_status` field reported through the probe; coordination consumer writes `task_prs` on `step.completed` with `ON CONFLICT DO NOTHING` idempotency and immediately calls `drain_pending_events`; `TREADMILL_AGENT_DRY_RUN=1` removed from CDK env; `LocalRuntime` pytest fixture + `wait_until_ready` helper at `tools/local-adapter/treadmill_local/pytest_harness.py` gated on `TREADMILL_LOCAL_HARNESS=1`; dispatcher honors `task_dependencies` via a blocked-clause SQL gate; dispatcher gates on plan-active via `plan_status.derived_status`; consumer runs a re-evaluation pass after relevant event handlers; `--dev` flag on CLI + API for local-only fast paths. **Phase 4 (real-Claude smoke + container integration + DLQ)**: the dry-run smoke is replaced as the gate for Phase 2 success criterion 4 by a real-Claude opt-in smoke at `workers/agent/tests/test_integration_real_claude.py` gated on `TREADMILL_REAL_CLAUDE=1` — cheapest model, trivial prompt, real bare repo, asserts authored file change + commit + push; worker container integration test at `workers/agent/tests/test_integration_container.py` gated on `TREADMILL_INTEGRATION=1` brings up the substrate via the C.7 fixture and runs the agent container once end-to-end; DLQ + redrive policy on coordination (`maxReceiveCount=5`) + work (`maxReceiveCount=3`) queues with 14-day retention; poison-message DLQ behavioral test at `services/api/tests/test_integration_dlq.py`. **New test infrastructure introduced by this closure**: a unit-test layer for the dispatcher (`test_dispatch_unit.py`, fake publisher + fake SQS so failure paths are testable without moto); a unit-test layer for the consumer (`test_consumer_unit.py`, handler-level with stub `sessionmaker`); the `pytest_harness.local_substrate` fixture; the real-Claude opt-in smoke; the worker container integration test. **Aggregate test totals: 333 passed, 142 skipped** (cli 19; services/api 147 + 132 integration-gated skipped; workers/agent 84 + 3 skipped; tools/local-adapter 54 + 1 skipped; infra 23; tools/dev-hooks 6). The skipped tests are integration tests gated by `TREADMILL_INTEGRATION=1`, `TREADMILL_LOCAL_HARNESS=1`, `TREADMILL_REAL_CLAUDE=1`, or `TREADMILL_CLAUDE_BINARY_SMOKE=1`. **Explicitly deferred to Week 3 with tracking**: (a) **D.7 — `event_triggers` consumer (full evaluator)**, required for `pr_opened` → `wf-review` auto-fire — ships in Week 3 alongside `wf-review`'s prompt + the event-shape ADRs; the reviewer's "ship partial" recommendation was the collapse-then-restore pattern previously rejected per `docs/learnings/2026-05-07-collapse-then-restore.md`. (b) **Multi-tier dispatch** — `compute_tier` removed from the wire; column reserved in DB; future ADR adds back when a GPU tier or similar arrives. (c) **Real GitHub mode (`gh pr create` etc.)** — explicitly Phase 4 work per the parent plan's "Out of scope"; the worker raises on `REPO_MODE=github` until then. (d) **Auto-restart for the consumer task if it dies** — current behavior is operator-visible via `/health/ready` 503; future ADR adds supervisor + auto-restart. **Week 2 of this plan is now truly closed.** The closure plan satisfied every adversarial-review finding except those explicitly deferred above. The foundation drift the 2026-05-11 review surfaced — branch format, Pydantic-boundary asymmetry, `task_prs` writer never wired, dry-run-as-success — is gone. Week 3 (workflow plumbing) can build on the fixed foundation.
- **2026-05-08** Week 2 closed: **agent worker shipped**, **autoscaler-driven end-to-end smoke green**. New `workers/agent/` workspace member with six modules — `config` (env-driven settings), `api_client` (GET `/api/v1/steps/{id}` decoder over httpx), `eventbus` (boto3 SNS publish for `step.started/completed/failed` with worker-supplied `event_id`), `workspace` (per-step temp dir lifecycle), `git` (clone/branch/commit/push with `local` and `github` modes), `claude_code` (shell-out to the Claude Code CLI with `--model`, `--append-system-prompt`, prompt composed from plan intent + task + ordered skill content), and `runner` (the polling loop: receive claim → fetch context → publish `step.started` → execute → publish `step.completed`/`step.failed` → delete). New `treadmill-agent` Docker image installs git + `gh` + Node.js + Claude Code CLI; `git config --global --add safe.directory '*'` so workers can clone host-mounted bare repos. Mounting: the local adapter mounts `~/.claude/.credentials.json` (read-only) and `.treadmill-local/repos/` (read-write) only into the agent family — other families ship without volumes. New worker-facing API: `GET /api/v1/steps/{id}` returns the full WorkerContext (step + run + task + plan + role with resolved skills + hooks) so the worker does one round-trip to the API per step. New `treadmill-local repo init <owner/name>` provisions a host-side bare repo with a seed commit on `main` for `REPO_MODE=local`. Coordination consumer extended to idempotently INSERT Event rows for worker-origin events (Postgres `ON CONFLICT (id) DO NOTHING`), so the audit log is complete per ADR-0011 even though the worker writes events directly to SNS. CDK: replaces the `treadmill-noop-worker` family with `treadmill-agent` (cpu 512, mem 1024, env carries `WORK_QUEUE_URL`, `EVENTS_TOPIC_ARN`, `TREADMILL_API_URL`, `REPO_MODE=local`, `MAX_STEPS=1`, plus a spike-only `TREADMILL_AGENT_DRY_RUN=1` flag — written into a TODO so the next ADR removes it once we wire production credential management). Spike-only dry-run mode in the agent: when the env var is set, the runner skips the Claude Code call and writes a deterministic file under `.treadmill/<step_id>.md` so the smoke test exercises the full pipeline without burning LLM tokens. Tests: **32 unit tests** in `workers/agent/` (api_client decoder + http error handling, eventbus record shape + attributes + uniqueness + unwired-fallback, runner orchestration with stubbed primitives — happy path / failure path / max_steps / empty queue / malformed claim, branch + commit-message helpers, git ops against a real bare repo end-to-end including a clone-branch-commit-push-pr cycle, workspace cleanup honoring `KEEP_WORKSPACES`, claude_code prompt composition + binary lookup + non-zero-exit + arg passing via stub binary); **3 new local-adapter tests** for `repo init` (creates bare with main branch / idempotent / clone produces seed); **3 new integration tests** in `services/api/tests/test_integration_steps_router.py` (full WorkerContext returned with resolved skills + hooks in declared order, 404 on unknown step, ordering preserved with non-alphabetical skill positions); **2 new CDK assertion tests** for the agent task-def + env, and the `treadmill-noop-worker` reference replaced with `treadmill-agent` in `test_three_single_replica_services_exist`. **End-to-end smoke verified live**: `treadmill-local up` + `repo init smoke/repo` + alembic upgrade + POST `/api/v1/plans` with a one-task doc → dispatcher logs `step.ready` to events SNS + sends `{step_id}` claim to FIFO work queue → autoscaler scales the agent service from 0 → 1 → agent worker pulls the claim, fetches context via API, publishes `step.started`, clones the bare repo, writes the dry-run marker file, commits, pushes branch, publishes `step.completed` with output `{branch, commit_sha, summary}` → coordination consumer projects onto `workflow_run_steps.status='completed'` → `task_status` VIEW resolves to `done`. Bare repo received the branch; tree contains the dry-run marker + the seed README. **Workspace total: 295 passing** (cli 15 unit + 4 integration = 19; services/api 97 unit + 86 integration = 183; workers/agent 32 unit; tools/local-adapter 42 unit; infra 19 unit). **Phase 2 plan: Week 1 complete, Week 2 complete.**

## Post-mortem

(To be filled in when this plan transitions to `completed` or `abandoned`.)
