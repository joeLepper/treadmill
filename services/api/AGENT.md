# services/api

## Purpose

This directory contains the Treadmill API, the event-driven control plane that coordinates workflow execution across the system. It is the single source of truth for workflow state, manages task dispatch, tracks step lifecycle, handles GitHub webhook ingestion, and routes work to agent workers. The API is built on FastAPI and SQLAlchemy async, backed by Postgres for durability and Redis for high-frequency state like task queues.

## Key surfaces

- `treadmill_api/cli.py` — entry point; starts the FastAPI server that listens for webhooks and exposes `/plans`, `/tasks`, `/health` routes.
- `treadmill_api/events/` — event schema definitions (GitHub pushes, pull merges, plan documents, step lifecycle, task output). Event registry routes incoming webhooks to the appropriate consumer.
- `treadmill_api/dispatch.py` — task dispatch logic; decides when to enqueue a step, applies dependency gates, deduplicates by composite key (plan SHA + step name).
- `treadmill_api/database.py` — Postgres schema: workflows, plans, steps, tasks, mergeability state, long-lived task output.
- `treadmill_api/parsers/plan_doc.py` — parses `docs/plans/*.md` frontmatter and extracts the task tree structure.

## Recent changes

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
