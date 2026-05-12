# ADR-0020: Observability via OpenTelemetry + Grafana

- **Status:** accepted (phase 2 implemented; phases 3-7 plan-tracked)
- **Date:** 2026-05-12 (phase 2 landed in commit 3f64338; plan for the rest at `docs/plans/2026-05-12-observability-stack.md`)
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

### One observability stack per deployment, deployed identically (RAMJAC precedent)

Treadmill follows the RAMJAC pattern: **a single Grafana + Tempo + Loki + Prometheus + OTel Collector composition, defined once and deployed the same way per AWS deployment**. There is **no per-environment divergence** in stack shape — the same compose file (or CDK construct) stands up the same stack on the personal AWS account, on an eventual employer AWS account, and on whatever future TreadmillCloudFull tenant exists. The CLI knows where each deployment's stack lives via the per-deployment YAML (ADR-0016).

Concrete shape (mirroring RAMJAC's `infrastructure/observability/docker-compose.yml`):

- **OTel Collector** receives OTLP on `:4317` (gRPC) + `:4318` (HTTP). Forwards traces to Tempo, metrics to Prometheus, logs to Loki.
- **Loki** — log storage; mounts an EBS volume for persistence in the AWS deployment.
- **Prometheus** — metric storage; same.
- **Tempo** — trace storage; writes object blobs to an S3 bucket scoped per deployment.
- **Grafana** — UI; pre-provisioned with the three datasources at constant UIDs (`loki`, `prometheus`, `tempo`) plus per-workflow dashboards (`treadmill-overview.json`, `treadmill-claude-code.json`, etc.).

The deployable form is one CDK construct (e.g., `TreadmillObservabilityStack` or as part of `TreadmillCloudLite`). The construct uploads the compose-dir as an S3 asset; EC2 user-data downloads + runs `docker compose up -d` on a single t3 instance. **Per-env variation is limited to**: the S3 bucket name for Tempo, the deployment ID for dashboard variables, the Grafana admin password (from Secrets Manager), and operator alert webhook (also Secrets Manager). Everything else is identical — including the datasource UIDs, which are load-bearing because the CLI's URL construction depends on them.

### `fully_local` (moto) mode emits no telemetry

Per RAMJAC's pattern, the fully-local moto-substrate mode does **not** stand up a Grafana stack. The OTel SDK in workers + API reads `OTEL_EXPORTER_OTLP_ENDPOINT` from env; when unset (the fully-local default), the SDK silently no-ops. No collector container, no Grafana container, no laptop-resident observability complexity — fully-local stays moto-fast.

Operators who want to dev against Grafana while still iterating locally use **`dev_local` mode** (ADR-0016), which already routes through real AWS. In dev-local, `OTEL_EXPORTER_OTLP_ENDPOINT` is set in `~/.treadmill/<deployment_id>.yaml` to the deployment's collector endpoint, and the local-adapter injects it into the worker + API container env. Workers running on the laptop emit OTLP to the AWS-hosted collector for the deployment. **The same Grafana UI that production uses serves the dev-local operator** — same dashboards, same datasource UIDs, same queries.

### CLI as the access layer: `treadmill observe`

The CLI does not spawn or own the Grafana stack; it is a **read-only access layer** that opens Grafana panels. Subcommands (mirroring RAMJAC's `ramjac observe`):

- `treadmill observe dashboard [<dashboard-name>]` — opens the named dashboard (or the default overview) in the operator's browser.
- `treadmill observe logs --task <task-id>` — opens a Grafana Explore Loki query filtered by the task ID.
- `treadmill observe traces --task <task-id>` — same, for Tempo.
- `treadmill observe metrics --metric <metric-name>` — same, for Prometheus.
- `treadmill observe status` — checks reachability of the Grafana endpoint; reports the access method used (direct, SSM tunnel, etc.).

Access pattern: the deployment's Grafana is on a private subnet (single-operator scale doesn't need a public Grafana). The CLI tries direct reach first; if that fails it starts an SSM port-forward via `aws ssm start-session ... AWS-StartPortForwardingSessionToRemoteHost` to forward `localhost:3000` to the Grafana EC2's port 3000. Same access pattern RAMJAC uses. Tailscale fallback if the operator has it; not required.

The CLI knows the Grafana endpoint + EC2 instance ID from `~/.treadmill/<deployment_id>.yaml`, populated by `treadmill-local init` from the observability stack's CFN outputs.

### Datasource UIDs are constants — load-bearing

Both the Grafana provisioning and the CLI's URL construction depend on the three datasource UIDs being `loki`, `prometheus`, `tempo` (lowercase, no per-env suffix). Any future construct that wants to vary datasource UIDs per deployment breaks the CLI's link construction silently. Pinned by this ADR.

### Identical-emit principle, enforced (unchanged)

The principle the operator stated — "function as identically as possible whether running locally or in production" — translates concretely to:

- Workers + API code never branches on deployment_mode for telemetry. The OTel SDK is configured once at startup; the `OTEL_EXPORTER_OTLP_ENDPOINT` env var determines where data goes (and a missing endpoint produces a clean no-op).
- Span attribute names, metric names, log structured-field names are **fixed by ADR** (this one). They don't change between deployments. A dashboard built against one deployment's Grafana works against another deployment's Grafana without modification.
- The Grafana dashboard JSON definitions live in `infra/observability/dashboards/` and are provisioned identically by every deployment's Grafana instance at startup.
- Per-deployment variation is limited to: dashboard `${DEPLOYMENT_ID}` substitution, alert webhook secret, Tempo S3 bucket name. None of these affect the operator-facing UI.

### Bunkhouse precedent: deliberately diverging

Bunkhouse (per the operator's note) never got Claude Code observability right. The mechanism there was: worker stdout captured by ECS to CloudWatch; operator went to CloudWatch and grep'd. The chunking + buffering of `capture_output=True`-style worker code was the underlying issue. We're inverting both halves: stream the Claude Code output AND emit via structured OTel logs from the start. Bunkhouse's CloudWatch-only stack still works for non-Claude-Code logs; we deliberately add the Grafana layer because grep'ing CloudWatch is the operator pain point.

### RAMJAC precedent: adopted

RAMJAC ships a single docker-compose Grafana stack deployed identically per AWS env. One CDK construct uploads the compose dir + provisioning configs as an S3 asset; EC2 user-data downloads and runs `docker compose up -d`. One OTel collector per env on ECS Fargate, fanning out to that env's Grafana EC2. Local dev has no Grafana — `OTEL_EXPORTER_OTLP_ENDPOINT` is unset and the OTel SDK's no-op path handles it. CLI access is via `ramjac observe <subcommand> <env>`, which constructs Grafana Explore URLs and opens them in a browser after starting an SSM port-forward if needed. Datasource UIDs are constants (`tempo`, `loki`, `prometheus`) — both CLI and provisioning depend on the stability.

Treadmill cribs RAMJAC's pattern in full: one stack per deployment, identical shape across deployments, fully-local mode emits silently, CLI as the read-only access layer. The only adapter is that Treadmill's "deployment" granularity replaces RAMJAC's "env" granularity — one stack per deployment (personal-Treadmill has its own; future employer-Treadmill has its own), not one stack per dev/staging/prod.

## Trade-offs

- **`fully_local` mode is observability-blind.** No Grafana for moto-only iteration. Operators who need to *see* what a workflow did use `dev_local` against their personal AWS deployment. Trade-off accepted: keeping fully-local trivially-fast is more valuable than mirroring the Grafana stack on laptop.
- **A persistent EC2 instance per deployment.** Roughly ~$10/month for a t3.small or t3.medium per deployment. At personal scale (one deployment) that's a fixed cost; at multi-deployment (employer-Treadmill in a separate account) each gets its own. The alternative — Grafana Cloud as a shared sink — saves the EC2 cost but introduces a multi-tenancy concern (personal + employer data in one Grafana Cloud tenant) that violates ADR-0016's account-isolation discipline.
- **OTel SDK adds startup latency.** ~1-2 seconds at worker container start. Acceptable given workers already take ~5 seconds for the auth bootstrap + secret fetch.
- **Log volume grows fast with Claude Code streaming.** Each Claude Code run emits hundreds of lines. Loki's retention is configurable in the compose file; Q20.a still applies.
- **Token tracking is best-effort at v0** (claude --output-format json parse). If unavailable, banked as a "switch to the SDK" follow-up.
- **OTel logs are still beta in Python.** Stable enough for our use; pinning + occasional migration cost is acceptable.
- **The Grafana provisioning JSON is yet another schema to maintain.** Acceptable; dashboards are explicit operator-facing contract.
- **CLI tunneling via SSM adds setup-once friction.** The operator runs `aws ssm start-session ...` (transparently, behind `treadmill observe`) and depends on the EC2 instance role having SSM enabled. Worth it to avoid a public Grafana endpoint.

## Alternatives considered

- **Mirror the Grafana stack locally for fully-local mode.** The original draft of this ADR. Five docker containers come up alongside Postgres + Redis + API; operator opens `localhost:3000`. Rejected on operator review (2026-05-12): the "identical-in-all-envs" intent is better served by *not* running the stack locally and pointing dev-local workers at the AWS-hosted Grafana instead. Local-only mode gains nothing from a separate observability stack — the operator who wants observability runs against dev-local where the real stack lives.
- **Grafana Cloud as the single shared sink across deployments.** Saves EC2 cost; introduces a multi-tenancy concern that violates ADR-0016's account-isolation discipline (personal + employer data in one tenant). Rejected for personal-scale + multi-employer deployments. Re-examinable if Grafana Cloud Teams isolation is sufficient and a future operator wants the cost savings.
- **Loki via Promtail/Alloy scraping container stdout** (no OTel). Simpler — workers don't need OTel SDK; just log JSON to stdout and let Alloy scrape. Loses traces + metrics in the unified abstraction. Rejected: traces are load-bearing for the "submit → review" latency story; OTel unifies all three signals at small additional cost.
- **CloudWatch-only, no Grafana.** What bunkhouse does. Operator-stated pain point ("we never got Claude Code worker logs right"). Rejected: CloudWatch logs are queryable but grep'ing them through the AWS Console is the operator pain point. Grafana fixes that.
- **Direct Prometheus scrape + Loki Promtail + Jaeger** without OTel. Three SDKs + three pipelines. The whole point of OTel is one SDK / one pipeline. Rejected.
- **DataDog / Honeycomb / New Relic.** Commercial; would work fine. The operator hasn't named a vendor preference; OSS stack (Grafana family) is the RAMJAC precedent. Easy to swap exporters later if commercial wins on UX.
- **Skip token tracking entirely at v0.** The "bonus points if we can do token tracking" framing makes it optional. If path (1) above proves brittle, we drop it without rewriting the ADR.
- **One Grafana stack shared across deployments.** Personal-Treadmill + employer-Treadmill emit to a single Grafana. Cheaper but blends data across employer accounts (cost-attribution audit violations per ADR-0016 §"Multi-tenant via separate AWS accounts"). Rejected.

## Open questions

- **Q20.a — Loki retention policy + storage budget?** Loki's chunk store grows fast with streamed Claude Code logs. Default in the deployed stack: 7-day retention, EBS-backed filesystem storage, no compaction. Operator can extend if storage is cheap, shrink if not. Same defaults across deployments.
- **Q20.b — Does the autoscaler watch the OTel collector's health?** A dead collector means silent data loss. The autoscaler (ADR-0018) is already a supervisor process; it could probe collector health on each tick. Defer until the first time a collector crash bites us.
- **Q20.c — Token tracking at the OTel layer vs an in-Treadmill table?** Tokens-as-OTel-metrics gives Grafana dashboards. Tokens-as-events-table-rows gives correlated DB queries. Probably want both eventually; v0 commits to OTel only (cheaper to implement).
- **Q20.d — Trace context propagation through SQS message attributes?** OTel has a standard for this (`traceparent` header in message attributes). The dispatcher needs to inject it on send; the worker needs to extract on receive. Mechanical but easy to forget — call out in the impl plan.
- **Q20.e — Should the `events` table (audit log) also be queryable from Grafana?** A Postgres datasource in Grafana would unify with the OTel data. Useful operationally. Defer until the OTel layer is stable.
- **Q20.f — Should observability deploy as part of `TreadmillCloudLite` or as a sibling `TreadmillObservabilityStack`?** RAMJAC splits the observability stack from the main service stacks (separate CDK stack, separate EC2). For Treadmill: probably a sibling stack so observability deploys/teardowns independently of the messaging+queues stack. Decision to make in the implementation plan (currently PR #7); flagged here for parity with RAMJAC's separation.
- **Q20.g — SSM Session Manager prerequisites.** The CLI's tunnel pattern requires the operator's AWS profile to have SSM permissions and the EC2 to have an SSM-enabled role + agent installed. Both default-on for new instances launched by recent CDK versions, but worth documenting in the runbook.

## Consequences

- **New Python deps**: `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp`, `opentelemetry-instrumentation-fastapi`, `opentelemetry-instrumentation-sqlalchemy`, `opentelemetry-instrumentation-requests` (or `httpx`). API + worker `pyproject.toml` files get these as runtime deps.
- **New module**: `services/api/treadmill_api/observability.py` + `workers/agent/treadmill_agent/observability.py` — each module configures the OTel SDK from settings + provides a singleton tracer/meter/logger pair the rest of the package uses. Both modules share the same shape so refactoring later (lift into a shared package) is trivial.
- **`workers/agent/treadmill_agent/claude_code.py`** rewrites to `Popen` + line-streaming + OTel log emission. The `CodeAuthorResult.summary` stays as the joined stdout, so callers don't change.
- **`tools/local-adapter/treadmill_local/runtime.py`** does **not** add observability containers (revised from the prior draft). `OTEL_EXPORTER_OTLP_ENDPOINT` is injected from the deployment YAML in `dev_local` mode; in `fully_local` mode it stays unset so the SDK no-ops.
- **`infra/observability/`**: new top-level directory with `docker-compose.yml` (Grafana + Tempo + Loki + Prometheus + OTel Collector) plus provisioning configs (datasources at constant UIDs, dashboards, alert rules). One source of truth; deployed identically per Treadmill deployment.
- **New CDK construct** (likely `TreadmillObservabilityStack` per Q20.f) deploys the compose dir to an EC2 instance with EBS volumes for Loki/Prometheus + an S3 bucket for Tempo. The deployment's CFN outputs include `ObservabilityGrafanaHost` (private IP) + `ObservabilityCollectorEndpoint` (the OTLP URL) + `ObservabilityEc2InstanceId` (for SSM tunneling).
- **`treadmill-local init`** extends to read those outputs into the per-deployment YAML so the CLI's `treadmill observe` subcommands + the worker/API env injection find them.
- **New CLI subcommand**: `treadmill observe {dashboard,logs,traces,metrics,status,open}` per the RAMJAC pattern. Lives in `cli/treadmill_cli/observe.py` (sibling to `cli.py`).
- **Dashboard v0**: one dashboard with three rows — worker logs (Loki, filterable by `task_id`), worker run rate + duration (Prometheus), and a task lineage view (Tempo). Token usage on a sidebar panel if path (1) above works.
- This ADR is **not blocked** by ADR-0019 or ADR-0018. The observability stack can be added incrementally — for instance, the OTel SDK in workers + API can ship before the deployed Grafana stack exists (operators run with `OTEL_EXPORTER_OTLP_ENDPOINT` unset and silent emission until the stack lands).
