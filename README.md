# Treadmill

Treadmill is an opinionated agentic runner.

See [`docs/adrs/0001-treadmill-is-an-opinionated-agentic-runner.md`](docs/adrs/0001-treadmill-is-an-opinionated-agentic-runner.md) for the foundational decision.

## Layout

- `docs/` — ADRs, plans, learnings, diagrams.
- `infra/` — AWS CDK app (Python). Single source of truth for both local and AWS topology.
- `tools/local-adapter/` — Treadmill-native local adapter that interprets CDK synth output and provisions a moto + Docker substrate.
- `workers/noop/` — minimal worker container used by the spike.
- `.claude/skills/` — Treadmill's Claude Code skills (`/decide`, `/plan`, ...).

## Status

Pre-alpha. Currently spiking the local adapter — see [`docs/plans/2026-05-07-local-adapter-spike.md`](docs/plans/2026-05-07-local-adapter-spike.md).
