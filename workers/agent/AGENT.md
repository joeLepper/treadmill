# workers/agent

## Purpose

This directory contains the Treadmill agent worker, the execution substrate that polls the API for task assignments, orchestrates Claude Code within an isolated Git workspace, drives code changes through the repository, and publishes step lifecycle events back to the API. Each worker is an ECS task that exits after completing a single step, enabling stateless autoscaling driven by SQS queue depth. The worker implements the step-to-PR journey: fetch the task, reserve workspace isolation, run Claude Code, validate outputs, open/update the PR, and publish success or failure back to the API.

## Key surfaces

- `treadmill_agent/__main__.py` — entry point; orchestrates task fetch, workspace setup, Claude Code execution, PR interaction, and event publishing.
- `treadmill_agent/runner.py` — core step execution orchestrator; polls API for available tasks, manages the lifetime of a single step, coordinates dispositions (code, plan, validation, review, analysis).
- `treadmill_agent/runner_dispositions/` — pluggable step-type handlers: `code.py` (wf-author), `plan_doc.py` (wf-plan), `validation.py` (wf-validate), `review.py` (wf-review), `analysis.py` (wf-analyze).
- `treadmill_agent/workspace.py` — Git workspace lifecycle; creates isolated per-task directories, initializes repos, manages branch state, triggers Claude Code, captures outputs.
- `treadmill_agent/claude_code.py` — Wrapper that invokes the Claude Code CLI, parses its output, and routes to appropriate event channels.
- `treadmill_agent/startup_auth.py` — GitHub auth bootstrap. In `github_auth_mode='app'` the worker calls `bootstrap_github_auth_via_app` twice per lifecycle: once at startup (no repo → home-installation token) and again per task from `runner._handle_step` once `ctx.repo` is known (`repo='owner/name'` → token scoped to that repo's installation, so `gh` can clone / push outside the home installation).
- `treadmill_agent/validation_runtime.py` — Deterministic + LLM-judge check execution. `run_llm_judge` injects the touched components' AGENT.md content into the judge prompt (via `gather_agent_md_context`) so rule prompts that reference an `AGENT_MD` input (e.g. ADR-0030's docs-current-with-pr) actually see the documentation they're asked to evaluate.
- `treadmill_agent/judge_eval.py` — Evaluation harness (ADR-0053) used by the agentic judge-prompt optimizer as its scoring metric. `evaluate_judge_prompt(prompt, examples, *, model, timeout_seconds)` runs a candidate prompt over labeled examples via `claude_code.run_claude`, parses each verdict from the JSON envelope (more permissive than `validation_runtime`'s parser — no fixed `Literal`), and case-insensitively matches it against each example's `gold_verdict`. Vocabulary-agnostic, so it scores both validator judges (`pass`/`fail`) and the architect (`accept-as-is`/`amend`). Parse failures and `run_claude` exceptions are surfaced as `error=True` in `per_example` and count against the score.

## Recent changes

- ADR-0030 — `treadmill_agent/observability.py` now builds the OTLP span + metric exporters from `opentelemetry.exporter.otlp.proto.http.*` instead of `...proto.grpc.*`, drops the gRPC-only `insecure=` kwarg, and appends the per-signal paths `/v1/traces` / `/v1/metrics` to `OTEL_EXPORTER_OTLP_ENDPOINT`. The collector listens on `:4318` (HTTP/protobuf); the previous gRPC exporter couldn't speak that protocol, so every export — including the `record_token_usage` counters — silently failed.
- ADR-0053 — `judge_eval.evaluate_judge_prompt` lands as the scoring metric for the agentic judge-prompt optimizer (`role-prompt-optimizer` / `wf-tune-judge-prompts`). Mirrors `run_llm_judge`'s prompt composition (`## PR diff` etc.) and JSON-envelope parsing so labeled-example scoring tracks production judge invocation; tolerant of arbitrary verdict vocabularies so the same harness covers validator judges and the architect.
- ADR-0049 — App-mode workers re-mint a repo-scoped installation token per task. After `_handle_step` fetches the `WorkerContext` and before `_execute`, the runner calls `startup_auth.bootstrap_github_auth_via_app(settings=..., repo=ctx.repo)` so `gh` is authenticated against the task's repo's installation (not just the home installation the startup bootstrap configured). A mint failure publishes `step.failed` and leaves the SQS message in flight per ADR-0025 — it does not crash the worker outside the step boundary.
- ADR-0052 — `run_llm_judge` now walks each touched path up to the nearest ancestor AGENT.md and embeds the content under an `## AGENT_MD` section before the PR diff. Closes the docs-currency false-pass where the judge previously reported "no AGENT.md exists" because the input was never supplied.
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
