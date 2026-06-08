---
date: 2026-06-08
trigger: surprise
status: captured
related: DED Cloud Run dev deploy, medicoder_events 0.1.x
---

# Learning: medicoder_events OTel module crash-loops when OTEL_EXPORTER_OTLP_ENDPOINT is unset and google-cloud-trace is not installed

## Trigger
`diagnosis-entity-detector-dev` crash-looped on startup with `ModuleNotFoundError: No module named 'google.cloud.trace_exporter'`. The service showed `Ready: True` in Cloud Run (an old healthy revision was still live) but every new container exited(1) immediately. This was invisible until we checked logs.

## Observation
`medicoder_events/otel.py` (≥0.2.0) has two branches:
- `OTEL_EXPORTER_OTLP_ENDPOINT` set → OTLP/HTTP exporter (works)
- `OTEL_EXPORTER_OTLP_ENDPOINT` unset → `from google.cloud.trace_exporter import CloudTraceExporter` (the 0.1.0 fallback)

The DED Docker image includes `medicoder-events` but not `opentelemetry-exporter-gcp-trace` (which provides `google.cloud.trace_exporter`). With no OTEL collector endpoint configured in the dev Pulumi stack, the fallback path triggered and crashed every container.

## Generalization
When a package's startup path branches on an env var, the branch that doesn't require the env var must either be always-importable or catch its `ImportError` gracefully. A fallback that crashes on import is worse than no fallback at all — it takes down the service silently while the `Ready: True` indicator still points to the old (pre-crash) revision.

## Proposed rule
`medicoder_events/otel.py` (and any similar bootstrap module): wrap the CloudTraceExporter import in `try/except ImportError` and log a warning rather than crashing. Callers that need observability should set `OTEL_EXPORTER_OTLP_ENDPOINT`; absence of the env var and absence of the library should yield "no observability, service still starts."

## Proposed remediation
- Short-term: set `otelCollectorEndpoint` in `Pulumi.dev.yaml` so Cloud Run services get `OTEL_EXPORTER_OTLP_ENDPOINT` set (applied 2026-06-08).
- Long-term: patch `medicoder_events/otel.py` to catch `ImportError` on the CloudTraceExporter branch and log a warning.

## Notes
The `cloud_run_revision` log filter did not surface this until we explicitly checked with `severity>=WARNING`. Cloud Run's service-level health indicator (`Ready: True`) reflects the last successful revision, not the crash rate of new containers — check revision logs directly after each deploy.
