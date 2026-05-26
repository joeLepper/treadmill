"""OpenTelemetry SDK configuration for Treadmill worker.

Reads OTEL_EXPORTER_OTLP_ENDPOINT and OTEL_SERVICE_NAME from env.
When the endpoint is set, configures TracerProvider + MeterProvider + logger
handler and installs auto-instrumentations. When unset, SDK no-ops cleanly.
"""

from __future__ import annotations

import logging
import os
from typing import Any

_tracer_provider: Any = None
_meter_provider: Any = None
_initialized = False
_token_counters: dict[str, Any] = {}


def _configure_otel() -> None:
    """Configure OpenTelemetry SDK when OTEL_EXPORTER_OTLP_ENDPOINT is set."""
    global _tracer_provider, _meter_provider, _initialized

    if _initialized:
        return

    _initialized = True
    _token_counters.clear()
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    service_name = os.getenv("OTEL_SERVICE_NAME", "treadmill-worker")

    if not endpoint:
        _setup_noop()
        return

    from opentelemetry import metrics, trace
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor

    resource = Resource(attributes={SERVICE_NAME: service_name})
    base = endpoint.rstrip("/")

    _tracer_provider = TracerProvider(resource=resource)
    _tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=base + "/v1/traces"))
    )
    trace.set_tracer_provider(_tracer_provider)

    _meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=base + "/v1/metrics")
            )
        ],
    )
    metrics.set_meter_provider(_meter_provider)

    HTTPXClientInstrumentor().instrument()
    BotocoreInstrumentor().instrument()
    LoggingInstrumentor().instrument()


def _setup_noop() -> None:
    """When endpoint is unset, configure no-op providers."""
    global _tracer_provider, _meter_provider

    from opentelemetry.trace import NoOpTracerProvider
    from opentelemetry.metrics import NoOpMeterProvider

    _tracer_provider = NoOpTracerProvider()
    _meter_provider = NoOpMeterProvider()


def configure() -> None:
    """Initialize OpenTelemetry SDK configuration."""
    _configure_otel()


def get_tracer(name: str) -> Any:
    """Get a tracer by name."""
    if not _initialized:
        _configure_otel()
    return _tracer_provider.get_tracer(name)


def get_meter(name: str) -> Any:
    """Get a meter by name."""
    if not _initialized:
        _configure_otel()
    return _meter_provider.get_meter(name)


def get_logger(name: str) -> logging.Logger:
    """Get a logger by name."""
    return logging.getLogger(name)


def inject_trace_context() -> dict[str, dict[str, str]]:
    """Return SQS/SNS MessageAttributes carrying the current W3C traceparent.

    Empty dict when there is no active span (no-op OTel or untraced code).
    Callers spread the result into their MessageAttributes without guards.
    """
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator,
    )

    carrier: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(carrier)
    return {
        key: {"DataType": "String", "StringValue": value}
        for key, value in carrier.items()
    }


def extract_trace_context(message_attributes: dict[str, Any]) -> Any:
    """Extract W3C trace context from SQS MessageAttributes.

    Returns an OTel Context for ``tracer.start_as_current_span(context=...)``.
    Returns the ambient context when attributes carry no traceparent (older
    publishers or queues without propagation wired).
    """
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator,
    )

    carrier = {
        key: attrs.get("StringValue", "")
        for key, attrs in (message_attributes or {}).items()
        if key in ("traceparent", "tracestate")
    }
    return TraceContextTextMapPropagator().extract(carrier)


def record_token_usage(
    *,
    model: str,
    role: str,
    task_id: str,
    step_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int,
    cache_read_tokens: int,
) -> None:
    """Emit OTel counters for one Claude Code invocation's token usage.

    Four counters per call:
      treadmill.claude.tokens.input
      treadmill.claude.tokens.output
      treadmill.claude.tokens.cache_creation
      treadmill.claude.tokens.cache_read

    Each carries attributes model, role, task_id, step_id.
    Counters are created lazily on the first call and reused thereafter.
    The function is a no-op when OTEL_EXPORTER_OTLP_ENDPOINT is unset
    (no-op MeterProvider absorbs all calls silently).
    """
    if not _initialized:
        _configure_otel()

    if "input" not in _token_counters:
        meter = _meter_provider.get_meter("treadmill.claude")
        _token_counters["input"] = meter.create_counter(
            "treadmill.claude.tokens.input",
        )
        _token_counters["output"] = meter.create_counter(
            "treadmill.claude.tokens.output",
        )
        _token_counters["cache_creation"] = meter.create_counter(
            "treadmill.claude.tokens.cache_creation",
        )
        _token_counters["cache_read"] = meter.create_counter(
            "treadmill.claude.tokens.cache_read",
        )

    attrs = {"model": model, "role": role, "task_id": task_id, "step_id": step_id}
    _token_counters["input"].add(input_tokens, attrs)
    _token_counters["output"].add(output_tokens, attrs)
    _token_counters["cache_creation"].add(cache_creation_tokens, attrs)
    _token_counters["cache_read"].add(cache_read_tokens, attrs)
