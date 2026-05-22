# services/api

## Purpose

This directory contains the Treadmill API, the event-driven control plane that coordinates workflow execution across the system. It is the single source of truth for workflow state, manages task dispatch, tracks step lifecycle, handles GitHub webhook ingestion, and routes work to agent workers. The API is built on FastAPI and SQLAlchemy async, backed by Postgres for durability and Redis for high-frequency state like task queues.

## Key surfaces

- `treadmill_api/cli.py` — entry point; starts the FastAPI server that listens for webhooks and exposes `/plans`, `/tasks`, `/health` routes.
- `treadmill_api/events/` — event schema definitions (GitHub pushes, pull merges, plan documents, step lifecycle, task output). Event registry routes incoming webhooks to the appropriate consumer.
- `treadmill_api/events/schedule.py` — `ScheduledTick` payload (entity_type="schedule", action="tick") emitted by the scheduler on each cron fire (ADR-0035).
- `treadmill_api/scheduler/` — cron scheduler package (ADR-0035): `cron.py` (croniter wrapper), `policy.py` (deterministic jitter + quiet-hours + backoff, ported from RAMJAC), `runner.py` (`SchedulerRunner` — 30 s poll loop + 4 h missed-tick replay on startup; subprocess entrypoint now installs a rotating file handler + rate-limited error logger), `bounded_logging.py` (`configure_rotating_logging` + `RateLimitedErrorLogger`, mirroring the local-adapter helper but kept package-local so `treadmill_api` has no dependency on `treadmill_local`).
- `treadmill_api/dispatch.py` — task dispatch logic; decides when to enqueue a step, applies dependency gates, deduplicates by composite key (plan SHA + step name).
- `treadmill_api/database.py` — Postgres schema: workflows, plans, steps, tasks, mergeability state, long-lived task output.
- `treadmill_api/parsers/plan_doc.py` — parses `docs/plans/*.md` frontmatter and extracts the task tree structure.
- `treadmill_api/onboarding_store.py` + `treadmill_api/models/onboarding.py` — ADR-0050 onboarding persistence: repo config (mode + auto-merge block), wf-discover repo profile, and the S3 context-doc index. Typed columns + Postgres ARRAYs per ADR-0011 (no JSONB).
- `treadmill_api/routers/onboarding.py` — ADR-0051 onboarding HTTP surface: `POST /api/v1/onboarding/repos` accepts a discovered profile + optional mode, upserts the repo profile and config via `OnboardingStore`, and resolves mode via `repo_profile.recommend_mode` when omitted.
- `treadmill_api/coordination/triggers.py` — auto-merge trigger (ADR-0031) now also honors the ADR-0050 per-repo `auto_merge_blocked` config in addition to the plan-level flag. Both the deadline-arming path (`maybe_auto_merge_on_mergeable`) and the fire-time gate (`_check_still_mergeable_for_auto_merge`) consult `OnboardingStore.get_repo_config` via the `_repo_auto_merge_blocked` helper; missing config or any lookup error fails OPEN to preserve pre-ADR-0050 behavior.

## Recent changes

- PR — Scheduler subprocess (`treadmill_api.scheduler.runner`) now owns its log file via `scheduler/bounded_logging.py::configure_rotating_logging` (10 MB × 3 backup cap), reading the path from `TREADMILL_SCHEDULER_LOG_FILE` (parent passes it). The poll loop's error path goes through `RateLimitedErrorLogger`: first occurrence of a signature logs the full traceback, repeats collapse into periodic counted summaries, and a successful tick calls `reset()` so the next incident re-arms. Replaces the `logging.basicConfig` stdout setup that the parent used to capture via raw `open(SCHEDULER_LOG_FILE, "ab")`. Mirrors `tools/local-adapter/treadmill_local/subprocess_logging.py` in shape but does not import it (separate package).
- [#TBD](https://github.com/anthropics/treadmill/pull/TBD) — ADR-0051 onboarding router: new `POST /api/v1/onboarding/repos` endpoint persists a posted `RepoProfile` + `RepoConfig` via `OnboardingStore`, honoring an explicit `mode` or falling back to `recommend_mode`.
- [#TBD](https://github.com/anthropics/treadmill/pull/TBD) — ADR-0050 d.5: live auto-merge trigger reads the per-repo `auto_merge_blocked` config from `OnboardingStore` and skips deadline-arm + fire-time merge if set; fails OPEN on missing config or lookup error.
- [#TBD](https://github.com/anthropics/treadmill/pull/TBD) — ADR-0050 onboarding persistence: new `repo_configs`, `repo_profiles`, `repo_context_docs` tables (typed columns + ARRAY per ADR-0011); `OnboardingStore` accessor over the existing `RepoConfig` / `RepoProfile` dataclasses.
- [#TBD](https://github.com/anthropics/treadmill/pull/TBD) — `treadmill_api/scheduler/` package: `SchedulerRunner` (30 s poll + 4 h replay), `cron.py` (croniter wrapper), `policy.py` (RAMJAC jitter + quiet hours + backoff), `events/schedule.py` (`ScheduledTick` payload registered in registry).
- [#38](https://github.com/anthropics/treadmill/pull/38) — AGENT.md schema document + validation rules.
- [#37](https://github.com/anthropics/treadmill/pull/37) — Document the post-deploy operator action for API credentials (ADR-0023 followup).
- [#33](https://github.com/anthropics/treadmill/pull/33) — First Treadmill-specific rules in `docs/knowledge-base/rules/`.

## Pitfalls

- Event idempotency is load-bearing: the API can receive duplicate webhook deliveries from GitHub; the dispatch deduplication key must be stable across replays. Changes to dispatch dedup logic are high-risk.
- Mergeability state is recomputed on every step output; this recomputation touches dependent tasks and can trigger cascading redispatches. Bugs in the mergeability transition logic can create infinite loops.
- Alembic migrations run at container startup; if a migration hangs or fails, the API container will not start and the entire system will block. Always test migrations locally with a representative Postgres size.
- The plan document parser reads only the frontmatter; changes to the frontmatter schema or parsing logic can cause valid old plans to stop being recognized. Test against plans in the wild before merging.

## Navigation

- **Adjacent:** `workers/agent/` (consumes task queue and publishes step outputs); `infra/` (this service is deployed by the CDK app).
- **Decisions:** ADR-0011 (event-driven, immutable runtime); ADR-0015 (multi-step workflows and role reuse); ADR-0017 (GitHub webhook ingestion); ADR-0021 (plan merge to main as submission trigger); ADR-0028 (DB-authoritative workflow configs).
- **Follow:** Start with ADR-0011 for the immutable runtime contract; read ADR-0015 for the plan + task + step hierarchy; trace a webhook through `events/github.py` → dispatcher → database.
