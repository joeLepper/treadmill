# ADR-0020: Observability via OpenTelemetry + Grafana

- **Status:** proposed
- **Date:** 2026-05-12
- **Related:** ADR-0011, ADR-0016, ADR-0018

## Context

Treadmill currently has the bare minimum observability: structured Python logging at INFO (since ADR-0019's friction-point fix) and the `events` table as audit trail. To debug or even just monitor a running deployment, the operator does some combination of `docker logs treadmill-api`, `psql ... SELECT * FROM events`, `aws sqs get-queue-attributes`. None of these compose; none of them survive a container restart in a useful form; none can answer questions like "what did the worker actually do during that 14-second Claude Code run?"

The operator has named the bunkhouse precedent failure mode explicitly: **we never got Claude Code worker output observability right**. The worker invokes Claude Code as a subprocess (see `workers/agent/treadmill_agent/claude_code.py:92`) with `capture_output=True` — which means the model output, tool calls, edits, and reasoning all go into a `result.stdout` string that the worker logs only as a one-line `summary` at the end. While Claude Code is running (often 30-60 seconds, sometimes longer), the operator sees nothing. If Claude Code times out or the worker crashes mid-run, the buffered output is lost. That's been a real pain point.

Three concrete needs:

1. **Logs.** Stream worker stdout (especially Claude Code subprocess output) to a queryable backend, tagged with `task_id` / `step_id` / `run_id` / `role` so the operator can reconstruct what a specific run did.
2. **Metrics.** Queue depths, worker run counts, step durations, success/failure rates, token consumption per task. Dashboards that show "the system is healthy / unhealthy" without grep'ing logs.
3. **Traces.** A single task's full lineage (submit → dispatch → worker pick-up → step execution → publish → trigger evaluator → next dispatch) as a connected trace, so latency analysis and failure root-causing don't require manual correlation across N log streams.

And one bonus: **token tracking.** Each Claude Code invocation consumes Anthropic API tokens. At single-operator personal scale, tokens map to operator budget. At any scale beyond that, tokens are the dominant cost driver — visibility is operationally required.

The architecture constraint the operator named: **whatever we build should function as identically as possible whether we're running locally or in production.** Two implications:

- The *emit* side (workers + API code) is identical regardless of where the data lands.
- The *collect* side (where data is stored + queried) can vary by deployment mode (local Grafana stack vs Grafana Cloud vs CloudWatch + AWS-hosted Grafana) — but only via configuration, not code changes.

## Decision

### OpenTelemetry as the universal emit-side abstraction

All three signals — logs, metrics, traces — are emitted via the OpenTelemetry (OTel) SDK in Python. The API and worker code is instrumented once; the *destination* of the data is determined entirely by the OTel collector's configuration, which differs per deployment mode.

Concretely:

- **Traces**: `opentelemetry.trace.get_tracer(__name__)` in every module that does meaningful work. The dispatcher creates a root span per task; the worker continues the trace by reading the trace context from the SQS message attributes. Every step is a span; every external call (Claude Code subprocess, gh CLI invocation, git push, SQS send, SNS publish) is a child span. Span attributes carry `task_id`, `step_id`, `run_id`, `role`, `repo`.
- **Metrics**: OTel metrics SDK with a counter for `worker.runs.total{outcome,role,workflow}`, a histogram for `worker.run.duration_seconds{role,workflow}`, a counter for `events.published.total{verb}`, a counter for `tokens.input{model,role}` + `tokens.output{model,role}` + `tokens.cache_read{model,role}` (Anthropic SDK gives all three). Queue depth comes from the autoscaler's existing `AutoscalerTick` — exposed as a gauge.
- **Logs**: OTel logs SDK (still beta in Python at time of writing; stable enough for our use). Logs carry the same span context as the surrounding trace, so a trace view in Grafana surfaces correlated log lines without joins.

### Claude Code subprocess: stream-and-tag, not capture-and-summarize

The current `claude_code.py:run_claude_code` uses `subprocess.run(..., capture_output=True)`. Replace with `subprocess.Popen` + stream reading: a background thread reads `claude`'s stdout line-by-line, emits each line as a log record (OTel log with `task_id` / `step_id` / `role` attributes + span context), and accumulates the full output to return as the summary. The worker's perception of "Claude Code finished and produced this summary" is unchanged; what changes is that **every intermediate line of model output, every tool call, every edit is visible live in Grafana keyed to the running step**.

This is the load-bearing improvement over bunkhouse. The operator can open a Grafana log panel, filter on `task_id=<the-id>`, and watch Claude Code's reasoning + tool calls scroll by in real time. When something goes wrong, the last few lines before the failure are right there, not lost in a discarded subprocess buffer.

### Token tracking: shell out to `claude` then parse, or move to the SDK?

Claude Code's CLI doesn't expose token counts on stdout. Three paths:

1. **Parse the `claude` CLI's JSON output mode**, if it has one. The newer versions of `claude` support `--output-format json` which includes `usage` stats per response. Verify availability + version-pin in the worker Dockerfile, then parse + emit as metrics.
2. **Replace the subprocess with direct Anthropic SDK calls.** Major refactor — `claude` CLI bundles a lot of tool-use machinery (file edits, bash execution, etc.) that we'd have to reimplement. Out of scope for v0.
3. **Skip token tracking at v0; instrument it when (2) becomes attractive for other reasons.**

This ADR commits to path (1) for v0. If `claude --output-format json` is available + stable, parse it; emit `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` as OTel counters with `model` + `role` + `task_id` attributes. If the output mode isn't reliable, fall back to (3) with an open task to revisit.

### Local stack: docker-composed Grafana + Loki + Prometheus + Tempo + OTel Collector

`treadmill-local up` adds five new containers in dev-local and fully-local modes:

- `treadmill-otel-collector` — receives OTLP on `:4317` (gRPC) + `:4318` (HTTP). Forwards traces to Tempo, metrics to Prometheus, logs to Loki.
- `treadmill-loki` — log storage on `:3100`.
- `treadmill-prometheus` — metric storage on `:9090`. Scrapes OTel Collector's `/metrics` endpoint as well as direct from API + workers if needed.
- `treadmill-tempo` — trace storage on `:3200`.
- `treadmill-grafana` — UI on `:3000`. Pre-provisioned with Loki + Prometheus + Tempo datasources + a starter dashboard.

The operator opens `http://localhost:3000` after `up` to see worker logs, metrics, traces. No additional commands.

Workers + API discover the OTel Collector via the container DNS name (`treadmill-otel-collector:4317`). The OTel SDK is configured via `OTEL_EXPORTER_OTLP_ENDPOINT` env var, set by the local-adapter when spawning containers.

A `--no-observability` flag on `up` lets the operator skip the Grafana stack when they just want to run workflows + don't care about the metric/log/trace ingest. Containers without the collector silently no-op the OTel SDK exports (OTel handles missing endpoints gracefully by default; we configure this explicitly).

### Production stack: same emitter, different exporters

In `fully_remote` mode (future ADR), the workers + API run as ECS tasks. The observability stack options:

1. **Grafana Cloud + OTel Collector ECS sidecar**. The collector forwards directly to Grafana Cloud's OTLP ingest. Single backend; matches local Grafana UX 1:1. Cost: Grafana Cloud's free tier covers personal scale; small monthly cost at small-team scale.
2. **AWS-managed**: CloudWatch Logs + CloudWatch Metrics + AWS X-Ray. The OTel Collector sidecar splits the three signals to the three AWS services. Operator queries via Grafana with the CloudWatch + X-Ray datasources installed.

This ADR doesn't commit to either; the eventual `TreadmillCloudFull` ADR makes the call. Both options share the same emitter side (workers + API code is identical), which is the constraint that mattered.

### Identical-emit principle, enforced

The principle the operator stated — "function as identically as possible whether running locally or in production" — translates concretely to:

- Workers + API code never branches on deployment_mode for telemetry. The OTel SDK is configured once at startup; environment variables determine where data goes.
- Span attribute names, metric names, log structured-field names are **fixed by ADR** (this one). They don't change between local and prod. Dashboards built against local Grafana keep working when pointed at the prod Grafana.
- The Grafana dashboard JSON definitions live in `infra/observability/dashboards/` and are loaded by both the local Grafana container (via volume mount) and the prod Grafana instance (via provisioning).

### Bunkhouse precedent: deliberately diverging

Bunkhouse (per the operator's note) never got Claude Code observability right. The mechanism there was: worker stdout captured by ECS to CloudWatch; operator went to CloudWatch and grep'd. The chunking + buffering of `capture_output=True`-style worker code was the underlying issue. We're inverting both halves: stream the Claude Code output AND emit via structured OTel logs from the start. Bunkhouse's CloudWatch-only stack still works for non-Claude-Code logs; we deliberately add the Grafana layer because grep'ing CloudWatch is the operator pain point.

## Trade-offs

- **Five new docker containers in local mode.** Memory cost ~2-3 GiB resident for the Grafana stack at idle. Acceptable on a modern laptop; the `--no-observability` flag handles the "I just want to run a workflow, don't bring the stack up" case.
- **OTel SDK adds startup latency.** ~1-2 seconds at worker container start. Acceptable given workers already take ~5 seconds for the auth bootstrap + secret fetch.
- **Log volume grows fast with Claude Code streaming.** Each Claude Code run emits hundreds of lines (tool calls, reasoning, edits). Loki's retention defaults (~7 days local) need explicit configuration to keep storage bounded. Open question Q20.a covers this.
- **Token tracking is best-effort at v0** (path 1 above). If `claude --output-format json` is unavailable or unstable, tokens get banked as a "we'll do this when we switch to the SDK" follow-up.
- **OTel logs are still beta in Python.** Stable enough for our use; if the SDK churns under us, we accept some pinning + occasional migration cost.
- **The Grafana provisioning JSON is yet another schema to maintain.** Acceptable; dashboards are explicit operator-facing contract anyway.

## Alternatives considered

- **Loki via Promtail/Alloy scraping container stdout** (no OTel). Simpler — workers don't need OTel SDK; just log JSON to stdout and let Alloy scrape. Loses traces + metrics in the unified abstraction. Rejected: traces are load-bearing for the "submit → review" latency story; OTel unifies all three signals at small additional cost.
- **CloudWatch-only, no Grafana.** What bunkhouse does. Operator-stated pain point ("we never got Claude Code worker logs right"). Rejected as the local stack; CloudWatch stays as a prod-side option behind the OTel Collector.
- **Direct Prometheus scrape + Loki Promtail + Jaeger** without OTel. Three SDKs + three pipelines. The whole point of OTel is one SDK / one pipeline. Rejected.
- **DataDog / Honeycomb / New Relic.** Commercial; would work fine. The operator hasn't named a vendor preference; OSS stack (Grafana family) is the safer default for personal scale. Easy to swap exporters later if commercial wins on UX.
- **Skip token tracking entirely at v0.** The "bonus points if we can do token tracking" framing makes it optional. If path (1) above proves brittle, we drop it without rewriting the ADR.

## Open questions

- **Q20.a — Loki retention policy + storage budget?** Loki's chunk store can grow fast with streamed Claude Code logs. Local default: 7-day retention, filesystem storage, no compaction. Prod: TBD by `TreadmillCloudFull`. Operator should set explicit limits before this lands so the local stack doesn't fill the laptop disk silently.
- **Q20.b — Does the autoscaler watch the OTel collector's health?** A dead collector means silent data loss. The autoscaler (ADR-0018) is already a supervisor process; it could probe collector health on each tick. Defer until the first time a collector crash bites us.
- **Q20.c — Token tracking at the OTel layer vs an in-Treadmill table?** Tokens-as-OTel-metrics gives Grafana dashboards. Tokens-as-events-table-rows gives correlated DB queries. Probably want both eventually; v0 commits to OTel only (cheaper to implement).
- **Q20.d — Trace context propagation through SQS message attributes?** OTel has a standard for this (`traceparent` header in message attributes). The dispatcher needs to inject it on send; the worker needs to extract on receive. Mechanical but easy to forget — call out in the impl plan.
- **Q20.e — Should the `events` table (audit log) also be queryable from Grafana?** A Postgres datasource in Grafana would unify with the OTel data. Useful operationally. Defer until the OTel layer is stable.

## Consequences

- **New Python deps**: `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp`, `opentelemetry-instrumentation-fastapi`, `opentelemetry-instrumentation-sqlalchemy`, `opentelemetry-instrumentation-requests` (or `httpx`). API + worker `pyproject.toml` files get these as runtime deps.
- **New module**: `services/api/treadmill_api/observability.py` + `workers/agent/treadmill_agent/observability.py` — each module configures the OTel SDK from settings + provides a singleton tracer/meter/logger pair the rest of the package uses. Both modules share the same shape so refactoring later (lift into a shared package) is trivial.
- **`workers/agent/treadmill_agent/claude_code.py`** rewrites to `Popen` + line-streaming + OTel log emission. The `CodeAuthorResult.summary` stays as the joined stdout, so callers don't change.
- **`tools/local-adapter/treadmill_local/runtime.py`** gains the five observability container specs + provisions them in both fully_local and dev_local modes. The `--no-observability` flag is wired through `up`.
- **`infra/observability/`**: new top-level directory with Grafana dashboard JSON + Prometheus scrape config + OTel collector config. Both local and (future) prod load from here.
- **Dashboard v0**: one dashboard with three rows — worker logs (Loki, filterable by `task_id`), worker run rate + duration (Prometheus), and a task lineage view (Tempo). Token usage on a sidebar panel if path (1) above works.
- This ADR is **not blocked** by ADR-0019 or ADR-0018. The observability stack can be added incrementally — for instance, Grafana + Loki + Alloy scraping container stdout could ship first (gives operator the Claude Code visibility win immediately), then Prometheus + Tempo + OTel SDK instrumentation come second.
