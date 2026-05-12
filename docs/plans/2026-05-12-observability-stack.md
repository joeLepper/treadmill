---
status: active
trigger: ADR-0020 phase 2 implemented + RAMJAC-precedent reframe 2026-05-12; operator approval 2026-05-12; PR #7 merged but plan-doc trigger race-lost on workflow seed (DB was fresh; seed-starters ran after the trigger) — re-merging now that workflows are seeded
parent: docs/plans/2026-05-13-week-4-dev-local-deployment.md
---

# Plan: observability stack — ADR-0020 phases 3+

ADR-0020 commits to OpenTelemetry as the universal emit-side
abstraction and a Grafana + Loki + Prometheus + Tempo + OTel Collector
stack deployed identically per Treadmill deployment (RAMJAC
precedent — see `docs/adrs/0020-observability-via-opentelemetry-and-grafana.md`
§"One observability stack per deployment, deployed identically").
Phase 2 — line-streaming Claude Code subprocess output — landed in
commit `3f64338` and proved its value mid-smoke. The remaining phases
(3-7) take Treadmill from "grep `docker logs`" to "open Grafana via
the CLI and watch a task's full lineage."

**This plan is `status: drafting` deliberately.** It is not yet a
submission signal for Treadmill to execute. The shape needs an
operator review pass before it's merged. When the operator flips
`status: drafting` → `status: active` and merges, ADR-0021's trigger
fires and Treadmill picks up the task list below.

## Goal

After this plan executes:

1. The Treadmill API container, the worker container, and the
   autoscaler subprocess all emit OpenTelemetry traces + metrics +
   structured logs via OTLP. The emitter is identical across
   `fully_local`, `dev_local`, and future `fully_remote` modes — the
   destination is `OTEL_EXPORTER_OTLP_ENDPOINT` when set, or a no-op
   when unset (the fully-local default).
2. A new CDK construct, `TreadmillObservabilityStack`, deploys
   Grafana + Tempo + Loki + Prometheus + OTel Collector as a single
   docker-compose unit on one EC2 instance per Treadmill deployment.
   The compose dir is uploaded as an S3 asset; user-data downloads
   and runs `docker compose up -d`. EBS volumes back Loki +
   Prometheus; an S3 bucket backs Tempo. Datasource UIDs (`loki`,
   `prometheus`, `tempo`) are constants — the CLI depends on them.
3. `treadmill-local init` discovers the deployment's observability
   endpoints from CFN outputs and writes them into
   `~/.treadmill/<deployment_id>.yaml`. The local-adapter injects
   `OTEL_EXPORTER_OTLP_ENDPOINT` into the dev-local worker + API
   container env from the YAML.
4. A new CLI subcommand `treadmill observe
   {dashboard,logs,traces,metrics,status,open}` constructs Grafana
   Explore URLs and opens them in a browser, tunneling via
   `aws ssm start-session` if the Grafana EC2 is private.
5. Trace context propagates through SQS message attributes — a task
   submitted in the API produces a single connected trace covering
   API → consumer → worker → publish → consumer.
6. Token usage is captured per Claude Code invocation as an OTel
   metric tagged with `model` + `role` + `task_id` — **if** `claude
   --output-format json` is available and exposes `usage` fields. If
   not, the work is banked as a follow-up plan once we move to the
   Anthropic SDK directly.

## Constraints / scope

### In scope

- OpenTelemetry SDK setup in `services/api` and `workers/agent`.
- Auto-instrumentation for FastAPI, SQLAlchemy, httpx, boto3.
- Manual span instrumentation for the worker's runner + Claude Code
  subprocess + the autoscaler tick loop.
- `services/api/treadmill_api/observability.py` and a sibling
  `workers/agent/treadmill_agent/observability.py` — same shape, so a
  future lift into a shared package is trivial.
- `TreadmillObservabilityStack` CDK construct under
  `infra/treadmill_infra/stacks/observability.py` (per ADR-0020 Q20.f
  — sibling stack, not folded into CloudLite).
- The Grafana-stack compose file + provisioning configs under
  `infra/observability/`. Source of truth deployed verbatim to the
  EC2.
- Per-deployment YAML schema extension under `aws.`:
  `observability_collector_endpoint`, `observability_grafana_host`,
  `observability_ec2_id`, `observability_grafana_admin_secret_name`.
- A starter Grafana dashboard at
  `infra/observability/dashboards/treadmill-overview.json` (three
  rows: Loki logs by `task_id`, Prometheus worker-run rate +
  duration, Tempo trace search).
- A new CLI subcommand `treadmill observe` (sibling to
  `treadmill submit` / `treadmill plan`).
- Trace context propagation via SQS `MessageAttributes` (W3C
  traceparent header convention).
- Token tracking via `claude --output-format json` parse — best
  effort; bank the work if claude doesn't support it.

### Out of scope

- A local-laptop Grafana mirror for `fully_local` mode (explicitly
  rejected per ADR-0020 alternatives — see "Mirror the Grafana stack
  locally").
- Grafana Cloud (rejected per ADR-0020 alternatives — multi-tenancy
  across employer accounts).
- Custom alerting wiring (Grafana alerts, PagerDuty, etc.) beyond
  whatever the default Grafana provisioning ships. Slack webhook via
  Secrets Manager is allowed if trivially configurable.
- Migrating away from the `claude` CLI to the Anthropic SDK.
- Renaming or restructuring the events table for trace correlation.

## Sequence of work

```yaml
sequence_of_work:
  - id: otel-sdk-foundation
    title: Wire the OpenTelemetry SDK into API + worker
    workflow: wf-author
    intent: |
      Add the OpenTelemetry SDK + OTLP exporter + FastAPI/SQLAlchemy/
      httpx/boto3 auto-instrumentation as runtime deps in both
      ``services/api/pyproject.toml`` and ``workers/agent/pyproject.toml``.

      Create two new modules with identical shape:
      ``services/api/treadmill_api/observability.py`` and
      ``workers/agent/treadmill_agent/observability.py``. Each module:
        1. Reads ``OTEL_EXPORTER_OTLP_ENDPOINT`` and
           ``OTEL_SERVICE_NAME`` from env.
        2. When the endpoint is set, configures a ``TracerProvider``
           + ``MeterProvider`` + (logger handler) and installs the
           auto-instrumentations (FastAPI, SQLAlchemy, httpx,
           boto3-sqs, boto3-secretsmanager).
        3. When the endpoint is **unset**, the SDK no-ops cleanly —
           tracers / meters / loggers return no-op stubs that don't
           emit anything. This is the fully-local default (per
           ADR-0020 §"`fully_local` (moto) mode emits no telemetry").
        4. Exposes ``get_tracer(name)`` / ``get_meter(name)`` /
           ``get_logger(name)`` accessors the rest of the code uses.

      Call the configuration function from the API's
      ``treadmill_api/cli.py`` (uvicorn entrypoint, alongside the
      existing ``logging.basicConfig``) and from the worker's
      ``__main__.py`` (before the auth bootstrap).

      Add one custom span in each service that proves end-to-end
      emission when the endpoint is set: the API's lifespan startup
      emits a ``treadmill.api.startup`` span; the worker's runner
      emits a ``treadmill.worker.step`` root span for each step.

      Note: the OTel-logs Python SDK was beta when ADR-0020 was
      drafted. If it is now stable, use it. If not, configure the
      standard Python logger with a filter that injects the current
      span's trace_id + span_id as log-record attributes — the
      collector (provisioned in the construct task below) picks
      these up via its filelog receiver.
    scope:
      files:
        - services/api/pyproject.toml
        - workers/agent/pyproject.toml
        - services/api/treadmill_api/observability.py
        - workers/agent/treadmill_agent/observability.py
        - services/api/treadmill_api/cli.py
        - workers/agent/treadmill_agent/__main__.py
        - services/api/treadmill_api/app.py
        - workers/agent/treadmill_agent/runner.py
        - services/api/tests/test_observability.py
        - workers/agent/tests/test_observability.py
    validation:
      - kind: deterministic
        description: |
          Both ``treadmill_api.observability`` and
          ``treadmill_agent.observability`` import cleanly and expose
          ``get_tracer`` / ``get_meter`` / ``get_logger``.
      - kind: deterministic
        description: |
          When ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set to a stub
          collector, an API request and a worker step each produce
          at least one span. When the env var is unset, neither
          service errors at startup and no spans are emitted.

  - id: observability-cdk-construct
    title: TreadmillObservabilityStack — one stack per deployment, deployed identically
    workflow: wf-author
    depends_on:
      - otel-sdk-foundation
    intent: |
      Per ADR-0020 §"One observability stack per deployment, deployed
      identically" and Q20.f: build a new CDK construct
      ``TreadmillObservabilityStack`` (sibling to
      ``TreadmillCloudLite``, not folded in) that deploys Grafana +
      Tempo + Loki + Prometheus + OTel Collector as a single
      docker-compose unit on one EC2 instance per Treadmill
      deployment.

      Required files under ``infra/observability/``:
        - ``docker-compose.yml`` — the five services + their
          inter-container DNS wiring. Each service is a Grafana-
          ecosystem image (otel/opentelemetry-collector-contrib,
          grafana/loki, prom/prometheus, grafana/tempo,
          grafana/grafana).
        - ``otel-collector-config.yaml`` — receives OTLP on :4317 +
          :4318; routes traces to Tempo, metrics to Prometheus, logs
          to Loki.
        - ``prometheus.yml`` — scrape jobs for the collector's own
          /metrics endpoint.
        - ``grafana/datasources.yml`` — three datasources with
          CONSTANT UIDs ``loki`` + ``prometheus`` + ``tempo`` (the
          CLI depends on these — do NOT vary per deployment).
        - ``grafana/dashboards.yml`` — file-provider config that
          loads any ``*.json`` from a sibling directory.
        - ``dashboards/treadmill-overview.json`` — starter dashboard
          (Loki logs by task_id, Prometheus worker rate + duration,
          Tempo trace search).

      The CDK construct itself:
        - Uploads ``infra/observability/`` as an S3 asset.
        - Launches one EC2 instance (t3.small default; configurable)
          with the SSM-enabled instance role.
        - User-data script: install docker; download the S3 asset;
          run ``docker compose up -d``.
        - Two EBS volumes: one for Loki chunks, one for Prometheus
          TSDB. Mount paths bind into the containers per the compose
          file.
        - One S3 bucket for Tempo blob storage; IAM-grant the EC2
          role read/write on it.
        - Security group: no public ingress except from the
          operator's IP (or via SSM session — preferred).
        - Grafana admin password generated as a Secrets Manager
          secret; injected into the compose env via SSM Parameter
          Store at user-data time.
        - CFN outputs: ``ObservabilityCollectorEndpoint`` (host:4317),
          ``ObservabilityGrafanaHost`` (private IP),
          ``ObservabilityEc2InstanceId``,
          ``ObservabilityGrafanaAdminSecretArn``.

      ``treadmill-local init`` extends to read the four new CFN
      outputs into the per-deployment YAML under ``aws.``:
      ``observability_collector_endpoint``,
      ``observability_grafana_host``, ``observability_ec2_id``,
      ``observability_grafana_admin_secret_name``.

      The local-adapter's ``_dev_local_api_env`` and
      ``_dev_local_worker_env`` inject
      ``OTEL_EXPORTER_OTLP_ENDPOINT`` from the YAML's
      ``observability_collector_endpoint`` when set. When unset
      (fully_local), the env var stays unset and the SDK no-ops.
    scope:
      files:
        - infra/observability/docker-compose.yml
        - infra/observability/otel-collector-config.yaml
        - infra/observability/prometheus.yml
        - infra/observability/grafana/datasources.yml
        - infra/observability/grafana/dashboards.yml
        - infra/observability/dashboards/treadmill-overview.json
        - infra/treadmill_infra/stacks/observability.py
        - infra/treadmill_infra/app.py
        - infra/tests/test_observability_stack.py
        - tools/local-adapter/treadmill_local/deployment_config.py
        - tools/local-adapter/treadmill_local/runtime.py
        - tools/local-adapter/tests/test_runtime_dev_local.py
        - tools/local-adapter/tests/test_deployment_config.py
    validation:
      - kind: deterministic
        description: |
          ``cdk synth TreadmillObservabilityStack --context
          mode=dev_local --context deployment_id=test`` produces a
          valid CloudFormation template with one EC2, the EBS
          volumes, the S3 bucket, and the four CFN outputs.
      - kind: deterministic
        description: |
          The Grafana provisioning config has three datasources with
          UIDs exactly ``loki``, ``prometheus``, ``tempo``. A
          regression test asserts this.
      - kind: deterministic
        description: |
          ``treadmill-local init <deployment_id>`` populates the four
          new YAML keys when run against a deployed stack; tested
          against mocked boto3 with a synthetic CFN response.
      - kind: deterministic
        description: |
          When the YAML's ``observability_collector_endpoint`` is
          set, the dev-local API + worker container env includes
          ``OTEL_EXPORTER_OTLP_ENDPOINT``. When absent (fully-local
          fixture), neither container env carries the var.

  - id: treadmill-observe-cli
    title: treadmill observe CLI subcommand — access-layer to Grafana
    workflow: wf-author
    depends_on:
      - observability-cdk-construct
    intent: |
      Per ADR-0020 §"CLI as the access layer: `treadmill observe`",
      add a new CLI subcommand group to ``cli/treadmill_cli/``:

        treadmill observe dashboard [--name <dashboard>]
        treadmill observe logs --task <task-id>
        treadmill observe traces --task <task-id>
        treadmill observe metrics --metric <metric-name>
        treadmill observe status
        treadmill observe open {dashboard|logs|traces|metrics} [args]

      Implementation (RAMJAC-precedent shape):
        - Read the per-deployment YAML to discover the Grafana host +
          EC2 instance ID.
        - Try direct reach on ``http://<grafana_host>:3000`` first.
        - If unreachable, start an SSM port-forward via
          ``aws ssm start-session ...
          AWS-StartPortForwardingSessionToRemoteHost`` for
          ``localhost:3000`` → ``<grafana_host>:3000``. Subprocess
          stays alive while the operator's session is active.
        - Construct Grafana Explore URLs with query params for the
          appropriate datasource (UIDs: ``loki``, ``prometheus``,
          ``tempo`` — constants per ADR-0020).
        - Shell out to ``webbrowser.open`` to launch the URL.

      ``treadmill observe status`` checks reachability + reports the
      access method used (direct vs SSM tunnel) without opening a
      browser. Useful for runbooks.

      The CLI does NOT spawn / start / manage the Grafana stack —
      that's the CDK construct's job.
    scope:
      files:
        - cli/treadmill_cli/observe.py
        - cli/treadmill_cli/cli.py
        - cli/tests/test_observe.py
    validation:
      - kind: deterministic
        description: |
          ``treadmill observe status --deployment personal`` reports
          either ``direct`` or ``ssm-tunnel`` as the access method
          (mocked test against synthetic YAML + reachability stub).
      - kind: deterministic
        description: |
          ``treadmill observe logs --task <uuid> --deployment personal``
          constructs the expected Grafana Explore URL targeting the
          ``loki`` datasource with a query that filters on
          ``task_id="<uuid>"``.

  - id: trace-context-through-sqs
    title: Propagate trace context across the SQS hop
    workflow: wf-author
    depends_on:
      - otel-sdk-foundation
    intent: |
      OTel's W3C-traceparent propagator carries the trace ID + parent
      span ID across hops. SQS supports message attributes; the
      standard pattern is to inject ``traceparent`` (and
      ``tracestate`` if present) as a string attribute on
      ``send_message`` and extract it on ``receive_message``.

      Edit the publisher in
      ``services/api/treadmill_api/eventbus.py`` (and any peer module
      that sends to SQS) to inject the current span context into
      outgoing message attributes. Edit the consumer in
      ``services/api/treadmill_api/coordination/consumer.py`` and the
      webhook-inbox poller in
      ``services/api/treadmill_api/coordination/webhook_inbox.py`` to
      extract incoming context and continue the trace as a child
      span.

      Same on the worker: SQS receive in
      ``workers/agent/treadmill_agent/runner.py`` extracts the
      traceparent and starts a child span; SNS publish via
      ``workers/agent/treadmill_agent/eventbus.py`` injects.

      Validation: a smoke against the deployed Grafana stack from
      ``observability-cdk-construct`` produces a single connected
      Tempo trace covering the full chain.
    scope:
      files:
        - services/api/treadmill_api/eventbus.py
        - services/api/treadmill_api/coordination/consumer.py
        - services/api/treadmill_api/coordination/webhook_inbox.py
        - workers/agent/treadmill_agent/runner.py
        - workers/agent/treadmill_agent/eventbus.py
        - services/api/tests/test_trace_propagation.py
        - workers/agent/tests/test_trace_propagation.py
    validation:
      - kind: deterministic
        description: |
          When a span exists at the time of an SQS ``send_message``,
          the outgoing ``MessageAttributes`` carry a ``traceparent``
          key. The corresponding ``receive_message`` consumer
          extracts the value and the resulting span has the same
          trace_id as the producer's span.

  - id: token-tracking-via-claude-json
    title: Capture Claude Code token usage as OTel metrics
    workflow: wf-author
    depends_on:
      - otel-sdk-foundation
    intent: |
      Audit the ``claude`` CLI for ``--output-format json`` support
      at the version pinned in the worker Dockerfile. Run
      ``claude --output-format json --print "hello"`` and inspect the
      output. If the JSON includes ``usage`` fields
      (``input_tokens``, ``output_tokens``,
      ``cache_creation_input_tokens``, ``cache_read_input_tokens``),
      proceed. If it doesn't, persist a brief findings file at
      ``docs/learnings/2026-05-XX-claude-json-output-tokens.md``
      explaining the gap and the path forward (likely: migrate to the
      Anthropic SDK in a future plan); set this task's ``decision``
      to ``blocked``.

      If JSON output is usable: invoke ``claude`` with
      ``--output-format json``; parse the usage block; emit four OTel
      counters from the worker's observability module:
        - ``treadmill.claude.tokens.input``
        - ``treadmill.claude.tokens.output``
        - ``treadmill.claude.tokens.cache_creation``
        - ``treadmill.claude.tokens.cache_read``
      Each with attributes ``model``, ``role``, ``task_id``,
      ``step_id``. The streaming fix from ADR-0020 phase 2 stays
      intact — JSON output mode + line-streaming are not necessarily
      compatible; if they conflict, the audit findings explain the
      trade-off.
    scope:
      files:
        - workers/agent/treadmill_agent/claude_code.py
        - workers/agent/tests/test_claude_code.py
        - docs/learnings/
    validation:
      - kind: deterministic
        description: |
          Either: a smoke-test claude invocation produces a JSON
          envelope with parsable ``usage`` fields AND the
          ``treadmill.claude.tokens.*`` OTel counters fire on each
          claude invocation; OR a learning file at
          ``docs/learnings/2026-05-XX-claude-json-output-tokens.md``
          documents the gap and the task's ``decision`` is
          ``blocked``.
```
