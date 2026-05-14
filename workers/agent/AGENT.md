# workers/agent

## Purpose

This directory contains the Treadmill agent worker, the execution substrate that polls the API for task assignments, orchestrates Claude Code within an isolated Git workspace, drives code changes through the repository, and publishes step lifecycle events back to the API. Each worker is an ECS task that exits after completing a single step, enabling stateless autoscaling driven by SQS queue depth. The worker implements the step-to-PR journey: fetch the task, reserve workspace isolation, run Claude Code, validate outputs, open/update the PR, and publish success or failure back to the API.

## Key surfaces

- `treadmill_agent/__main__.py` — entry point; orchestrates task fetch, workspace setup, Claude Code execution, PR interaction, and event publishing.
- `treadmill_agent/runner.py` — core step execution orchestrator; polls API for available tasks, manages the lifetime of a single step, coordinates dispositions (code, plan, validation, review, analysis).
- `treadmill_agent/runner_dispositions/` — pluggable step-type handlers: `code.py` (wf-author), `plan_doc.py` (wf-plan), `validation.py` (wf-validate), `review.py` (wf-review), `analysis.py` (wf-analyze).
- `treadmill_agent/workspace.py` — Git workspace lifecycle; creates isolated per-task directories, initializes repos, manages branch state, triggers Claude Code, captures outputs.
- `treadmill_agent/claude_code.py` — Wrapper that invokes the Claude Code CLI, parses its output, and routes to appropriate event channels.

## Recent changes

- [#36](https://github.com/anthropics/treadmill/pull/36) — Local-adapter fetches API credentials at startup and injects them into the agent container environment.
- [#29](https://github.com/anthropics/treadmill/pull/29) — Worker validation_runtime — deterministic + llm-judge primitives for running validation rules as a disposition.
- [#28](https://github.com/anthropics/treadmill/pull/28) — Multi-step workflow support; task depends_on dependencies gated by API before dispatch.

## Pitfalls

- Each worker MUST exit after one step (EXIT_AFTER_STEP=true); persistent workers hide exit-path bugs that only surface in production autoscale churn. Never disable this in dev unless tracing a specific bug.
- Claude Code subprocess output parsing is fragile; changes to the Claude Code harness output format can break the worker silently (no exception, just lost events). Always test against a real Claude Code run after format changes.
- The workspace directory is created under `/tmp` and must be isolated per task to avoid concurrent writes during SQS-driven autoscaling. Cross-task workspace collisions cause data loss.
- Git credential injection via `startup_auth.py` happens once at container startup; if a step changes a secret or credential strategy mid-task, those changes will not be reflected in subsequent git operations within the same step.

## Navigation

- **Adjacent:** `services/api/` (consumes task queue and publishes step outputs); `infra/` (this worker is defined as an ECS task construct); `tools/local-adapter/` (worker container is deployed locally via the adapter).
- **Decisions:** ADR-0011 (event-driven, immutable runtime); ADR-0015 (multi-step workflows); ADR-0019 (host-side credential injection); ADR-0022 (role output kinds).
- **Follow:** Read ADR-0011 for the step lifecycle contract; trace a task through `runner.py` → `runner_dispositions/code.py` → `workspace.py` → `claude_code.py`.
