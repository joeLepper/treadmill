---
auto_merge: true
status: active
---

# Plan: Fix OTel export — use the HTTP exporter (unblock the o11y stack)

- **Status:** active
- **Date:** 2026-05-22
- **Related ADRs:** ADR-0020 (observability via OTel + Grafana), ADR-0043 (dev-local o11y)

## Goal

The o11y stack receives **zero app telemetry**: both `services/api` and
`workers/agent` instantiate the **gRPC** OTLP exporter
(`opentelemetry.exporter.otlp.proto.grpc...`) but the deployment sets
`OTEL_EXPORTER_OTLP_ENDPOINT=http://treadmill-otel-collector:4318` (HTTP port;
gRPC needs 4317) + `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`. Every export fails
`StatusCode.UNAVAILABLE` and floods the logs; Prometheus has 0 series for
`treadmill_claude_tokens_input_total` (verified). Switch both exporters to the
**HTTP/protobuf** OTLP exporter so traces + metrics actually reach the collector —
the keystone for every dashboard (latency, the token panels, everything).

## Success criteria

- API + worker use the HTTP OTLP exporter (matching `:4318` + `http/protobuf`).
- After deploy, Prometheus shows `treadmill_claude_tokens_input_total` series
  (operator confirms post-deploy with a worker run) and the API log no longer
  floods OTLP `UNAVAILABLE`.
- Existing observability setup + tests stay green.

## Constraints / scope

### In scope
The exporter class swap (gRPC → HTTP) in both `observability.py` files + tests +
docs. The HTTP exporter package (`opentelemetry-exporter-otlp-proto-http`) is
already installed in both images (verified) — no dependency change expected;
add it to the relevant `pyproject.toml` only if an import actually fails.

### Out of scope
Dashboard panels (separate token Wave 2), the collector config, switching to
SDK env-driven auto-config (keep explicit exporter construction).

### Budget
One task, `auto_merge: true`. Touches `services/api` + `workers/agent`
`observability.py` (low overlap with the concurrent session). After merge it
needs both images rebuilt + redeploy to take effect.

## sequence_of_work

```yaml
sequence_of_work:
  - id: otel-http-exporter
    title: Switch API + worker OTLP exporters from gRPC to HTTP (ADR-0020)
    workflow: wf-author
    intent: |
      Both ``services/api/treadmill_api/observability.py`` and
      ``workers/agent/treadmill_agent/observability.py`` build the OTLP exporters
      from ``opentelemetry.exporter.otlp.proto.grpc.{trace,metric}_exporter``
      with ``OTLPSpanExporter(endpoint=endpoint, insecure=True)`` /
      ``OTLPMetricExporter(endpoint=endpoint, insecure=True)`` where ``endpoint``
      is ``OTEL_EXPORTER_OTLP_ENDPOINT`` (= ``http://treadmill-otel-collector:4318``,
      the HTTP port). gRPC can't talk to 4318 → all exports fail. Switch BOTH
      files to the HTTP exporter. CRITICAL DETAILS (a naive import swap breaks):

        (1) IMPORTS: change ``...proto.grpc.trace_exporter`` →
        ``opentelemetry.exporter.otlp.proto.http.trace_exporter`` and
        ``...proto.grpc.metric_exporter`` →
        ``opentelemetry.exporter.otlp.proto.http.metric_exporter`` (the class
        names ``OTLPSpanExporter`` / ``OTLPMetricExporter`` are the same).

        (2) NO ``insecure=`` KWARG: the HTTP exporter does NOT accept
        ``insecure`` (that's gRPC-only) — remove it. Passing it raises TypeError.

        (3) ENDPOINT PER-SIGNAL PATH: the HTTP exporter needs the full signal
        path, unlike gRPC which used the base URL. Build:
        ``OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")`` and
        ``OTLPMetricExporter(endpoint=endpoint.rstrip("/") + "/v1/metrics")``.
        (If a logs exporter is also constructed there, use ``/v1/logs``.)

      Apply the SAME change to BOTH files. Keep everything else (resource,
      providers, processors, instrumentation, the ``record_token_usage``
      counters) unchanged. The ``opentelemetry-exporter-otlp-proto-http`` package
      is already installed in both images; only touch ``pyproject.toml`` if an
      import genuinely fails.

      TESTS: in each package, add/extend a test asserting the configured span
      exporter is an instance of the HTTP ``OTLPSpanExporter`` (import from
      ``...proto.http.trace_exporter``) — e.g. set ``OTEL_EXPORTER_OTLP_ENDPOINT``
      to a dummy URL, call the setup function, and assert the exporter class
      module is ``...proto.http...`` (and that no ``insecure`` TypeError occurs).
      Keep existing observability tests green.

      DOCS (ADR-0030 — REQUIRED): note in BOTH ``services/api/AGENT.md`` and
      ``workers/agent/AGENT.md`` that OTLP export now uses the HTTP/protobuf
      exporter against ``:4318`` (was gRPC against the HTTP port → silent
      export failure).
    scope:
      files:
        - services/api/treadmill_api/observability.py
        - workers/agent/treadmill_agent/observability.py
        - services/api/tests/
        - workers/agent/tests/
        - services/api/AGENT.md
        - workers/agent/AGENT.md
    validation:
      - kind: deterministic
        description: |
          Both observability modules import the HTTP OTLP exporter and their
          tests pass.
        script: |
          grep -q "proto.http.trace_exporter" services/api/treadmill_api/observability.py \
            && grep -q "proto.http.trace_exporter" workers/agent/treadmill_agent/observability.py \
            && ! grep -q "insecure=True" services/api/treadmill_api/observability.py \
            && cd services/api && uv run pytest tests/ -q -k "observability or otel" \
            && cd ../../workers/agent && uv run pytest tests/ -q -k "observability or otel"
```

## Risks / unknowns

- **Logs exporter:** if either file also builds an OTLP *logs* exporter, switch
  it to HTTP + ``/v1/logs`` the same way.
- **Deploy to take effect:** both images must rebuild + redeploy; the operator
  confirms Prometheus shows token series + the API OTLP flood stops.

## Post-mortem

_(filled when the wave completes)_
