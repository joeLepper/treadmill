# AGENT.md schema

Agent documentation (`AGENT.md`) files live at component roots and answer three questions: **What is this?** **How does an agent interact with it?** **What gotchas exist?** Each `AGENT.md` contains exactly five required sections; within each section, content is free-form prose.

## Required sections

### Purpose
One paragraph naming what lives in this directory. Answer: what is the architectural role or domain responsibility of this component? For example:

> This directory contains the Treadmill CDK app, the single source of truth for both local (moto + Docker) and AWS (CloudFormation) topology. It synthesizes into a provider-agnostic intermediate format (CDK JSON) that the local adapter interprets and the AWS CDK CLI deploys.

### Key surfaces
Load-bearing files and their roles. Answer: if an agent lands here to make a change, which files matter most? What does each one do? Use a short list or paragraph format. For example:

> - `infra/app.py` — root CDK construct, assembles service + backing-service stacks.
> - `infra/services/api/` — service construct for the Treadmill API container.
> - `infra/services/api/handler.py` — Lambda@Edge handler for request routing; changes here affect deployed request paths.

### Recent changes
Notable recent changes with PR links. Answer: what has shifted recently that an agent touching this component should know? For example:

> - [#42](https://github.com/org/repo/pull/42) — Migrated autoscaler to ECS Scheduled Tasks (was cron Lambda).
> - [#38](https://github.com/org/repo/pull/38) — Added VPC endpoint for S3 to reduce NAT costs.
> - [#35](https://github.com/org/repo/pull/35) — Refactored service construct to use mixins (was copy-paste subclasses).

**Authoring flow (2026-06-12, task 986c5cf6):** new entries are PER-PR
FRAGMENT FILES, never in-file prepends. Add
`<component>/agent-changes/YYYY-MM-DD-<task-or-pr-slug>.md` beside the
AGENT.md — one entry per file, same content shape as above (what
changed, PR link). Newest-first falls out of filename sort. The slug
MUST begin with the dispatching task-id short form (8 hex chars, e.g.
`986c5cf6-…`) or the PR number (e.g. `341-…`): path uniqueness is
inherited from the allocator, so two same-day same-component PRs
cannot choose a colliding filename (a freeform slug would reopen the
add/add conflict this convention exists to kill). Prepending
entries directly into AGENT.md is the conflict factory that stacked
three same-day rework cascades on 2026-06-12: every in-flight PR
inserts at the same anchor, so every merge re-conflicts every open PR
(see `docs/learnings/2026-06-12-agent-md-recent-changes-is-a-conflict-factory.md`).
The `## Recent changes` section in each AGENT.md is a pointer to the
directory; entries written before the convention stay frozen in place.
Gardening a fragment = fold any load-bearing fact into the prose
sections above and delete the file (a deletion cannot conflict with
concurrent additions).

### Pitfalls
Gotchas, latent bugs, surprising behaviors, or traps that aren't obvious from reading the code. Answer: what does an agent need to know to avoid breaking this component? For example:

> - CDK does not validate Condition names at synth time; invalid condition names fail silently at deploy. See [infra/conditions.py](infra/conditions.py) comments.
> - The local adapter uses `docker run` not `docker-compose`. Do not add services to `docker-compose.yml`; instead add a CDK construct.
> - Lambda environment variables are case-sensitive in Python but the CDK CloudFormation layer uppercases them; test locally with a real Docker run.

### Navigation
Pointers to adjacent components and relevant ADRs. Answer: how does this component connect to others? What decisions govern it? For example:

> - **Adjacent:** `services/api/` (this CDK construct deploys the API service found there); `tools/local-adapter/` (interprets this construct).
> - **Decisions:** ADR-0002 (CDK as single source of truth for both local and AWS); ADR-0019 (host-side credential injection pattern used in infra/roles/).
> - **Follow:** Start with ADR-0002 to understand the provider-agnosticism contract.

## Example: minimal AGENT.md

```markdown
# tools/local-adapter

## Purpose

This directory interprets CDK synth output (provider-agnostic intermediate format) into a moto + native Docker substrate, enabling local-first development that mirrors AWS topology. It is the bridge between the CDK app and local `docker run` execution.

## Key surfaces

- `tools/local-adapter/app.py` — entry point; loads CDK JSON and dispatches to service adapters.
- `tools/local-adapter/adapters/` — per-service adapters that convert CDK constructs to Docker environment objects.
- `tools/local-adapter/moto_wrapper.py` — wraps AWS SDK calls to moto to simulate S3, SQS, etc. locally.

## Recent changes

- [#39](https://github.com/org/repo/pull/39) — Added credential injection from host environment into container.
- [#35](https://github.com/org/repo/pull/35) — Refactored adapters to use composition (was inheritance with trait-mixing).
- [#28](https://github.com/org/repo/pull/28) — Initial spike: moto + Docker Compose proof-of-concept.

## Pitfalls

- Moto does not implement all AWS API behaviors; validate against a real AWS environment before moving code to production.
- Docker resource limits are not validated at adapter compile time; `memory` and `cpu` fields in adapters are hints, not enforced. Test load locally with Docker stats.

## Navigation

- **Adjacent:** `infra/` (CDK app that this adapts); `workers/` (containers deployed by this adapter).
- **Decisions:** ADR-0002 (local-first + CDK as single source of truth); ADR-0011 (immutable event-driven runtime).
- **Follow:** ADR-0002 for the architecture; `tools/local-adapter/tests/` for examples of how adapters translate constructs.
```

## Convention: free-form prose within sections

Each section's content is prose, not a rigid schema. A "Purpose" might be one paragraph or three; a "Key surfaces" might be a list, a table, or a narrative paragraph. The agent that reads `AGENT.md` should be able to answer the five questions; the structure of the answer is secondary. This freedom allows each component to document its own idiom rather than forcing artificial uniformity.

**One constraint:** section headers must match the five names exactly at H2 (`## Purpose`, `## Key surfaces`, `## Recent changes`, `## Pitfalls`, `## Navigation`) so that validation rules can detect missing sections deterministically. Content within is free.
