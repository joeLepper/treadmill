# Treadmill — root agent context

## What is this directory?

Treadmill is an opinionated agentic runner. We build systems that produce software with minimal human intervention by baking design, validation, documentation, and operations best practices into the runner itself. See [`docs/adrs/0001-treadmill-is-an-opinionated-agentic-runner.md`](docs/adrs/0001-treadmill-is-an-opinionated-agentic-runner.md) for the foundational decision.

The repo is a uv workspace. Three workspace members today:

- `infra/` — AWS CDK app (Python). Single source of truth for both local and AWS topology.
- `tools/local-adapter/` — Treadmill-native local adapter that interprets CDK synth output into a moto + Docker substrate. See ADR-0002.
- `workers/noop/` — minimal worker container used by the local-adapter spike.

Other top-level directories:

- `docs/` — Treadmill's own development records: ADRs, plans, learnings, diagrams about how Treadmill itself is built (Layer 1 per ADR-0003).
- `docs/knowledge-base/` — what Treadmill ships to projects it manages: cross-project policy ADRs, crystallized learnings, rules + remediations (Layer 3 per ADR-0003). Separate audience from `docs/`; do not mix.
- `.claude/skills/` — Treadmill's Claude Code skills (`/decide`, `/plan`, `/learning`).

## What conventions apply at this level?

**Documentation (ADR-0003 three-layer model).**
- ADRs at `docs/adrs/NNNN-slug.md`. Author via `/decide`. Numbered, immutable except for status header.
- Plans at `docs/plans/<date>-<slug>.md`. Author via `/plan`. Mutable while active; post-mortem on close.
- Learnings at `docs/learnings/<date>-<slug>.md`. Author via `/learning` (manual today; auto-trigger coming in ADR-0008).
- AGENTS.md per significant directory. Each answers three questions: what is this directory, what conventions apply here, what should be read first.

**Voice in documentation.**
Collective first-person plural ("we"). No personal names in prose; metadata fields are the place for accountability. See `/decide` skill for the full convention.

**Tests ship with features** (rule:`features-ship-with-tests`, crystallized from the learning of the same slug).
Production code under `infra/`, `tools/`, `workers/`, `services/` lands in the same session as the tests that exercise its primary success criteria. Spike code that intentionally elides tests must declare so explicitly in its plan. The rule has both a deterministic check (`tools/rule-checks/features-ship-with-tests/test-files-changed.sh`) and an LLM-judge check; remediations include `block-merge` on the deterministic failure.

**Auto-capture is on (ADR-0008).** A `UserPromptSubmit` hook at `tools/dev-hooks/capture_learning_candidate.py` scans every user prompt for correction-phrase triggers and surfaces matches as candidate learnings in `.treadmill-local/learning-candidates.jsonl`. When the hook injects `[treadmill auto-capture]` into your context, decide whether to author a `/learning` and flip the candidate's status accordingly. Sweep open candidates before ending a session.

**Treat structural separators as load-bearing (`docs/learnings/2026-05-07-collapse-then-restore.md`).** Before collapsing a directory, layer, parallel sequence, or distinct artifact type, name its apparent purpose explicitly and confirm with the human that the purpose has dissolved. "This seems redundant" is not justification.

**Single source of truth for AWS topology (ADR-0002).**
The CDK app at `infra/` describes both local and AWS deployments. We do not maintain a parallel `docker-compose.yml`. The local adapter at `tools/local-adapter/` interprets CDK synth output to operationalize the same topology against moto + native Docker.

**Workspace and tooling.**
- Python ≥3.12 everywhere. `uv` for package management. `uv sync --all-packages --all-groups` syncs the whole workspace including dev deps.
- `pytest` for tests. Test directories must NOT contain `__init__.py` — pytest in this monorepo uses rootdir-relative naming, and sibling `tests/` directories collide otherwise.
- Hatchling for builds, with packages declared via flat (non-`src/`) layout. Editable installs require this layout in our uv setup.
- Ruff for lint, configured at the repo root.

## What should an agent read first?

In order of importance for orientation:

1. [`docs/adrs/0001-treadmill-is-an-opinionated-agentic-runner.md`](docs/adrs/0001-treadmill-is-an-opinionated-agentic-runner.md) — what Treadmill is and why.
2. [`docs/adrs/0002-local-first-via-treadmill-native-cdk-adapter.md`](docs/adrs/0002-local-first-via-treadmill-native-cdk-adapter.md) — how the local substrate works.
3. [`docs/adrs/0003-three-layer-documentation-model.md`](docs/adrs/0003-three-layer-documentation-model.md) — how documentation is organized.
4. [`docs/plans/2026-05-07-local-adapter-spike.md`](docs/plans/2026-05-07-local-adapter-spike.md) — completed spike with running log + post-mortem; the most recent honest record of how work actually got done here.
5. [`docs/learnings/2026-05-07-features-ship-with-tests.md`](docs/learnings/2026-05-07-features-ship-with-tests.md) — the first captured learning, with proposed rule and remediation.

## Session end: sweep open candidates

When ending a session, the Stop hook (`tools/dev-hooks/review_candidates_at_stop.py`, registered in `.claude/settings.json`) injects a summary of open learning candidates remaining in `.treadmill-local/learning-candidates.jsonl`. Sweep them before stopping: capture promising ones via `/learning`, dismiss the rest with a brief note in the JSONL `notes` field and flip `status` from `open` to `captured` or `dismissed`. The candidates queue is the human-readable backlog for ADR-0008's auto-capture skill — leaving entries `open` indefinitely defeats the auto-capture loop.

## Local development

```bash
uv sync --all-packages --all-groups        # workspace sync incl. dev
uv run pytest tools/local-adapter/tests infra/tests   # full test suite

cd infra && JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION=1 uv run cdk synth
                                            # synth the CDK app

uv run treadmill-local up                   # bring up the local substrate
uv run treadmill-local status               # what's running
uv run treadmill-local run-worker treadmill-noop-worker
                                            # start one noop worker
uv run treadmill-local down                 # tear everything down
```

The autoscaler runs as a detached subprocess; its logs land at `.treadmill-local/autoscaler.log`.
