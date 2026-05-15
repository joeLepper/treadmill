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


def _configure_otel() -> None:
    """Configure OpenTelemetry SDK when OTEL_EXPORTER_OTLP_ENDPOINT is set."""
    global _tracer_provider, _meter_provider, _initialized

    if _initialized:
        return

    _initialized = True
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    service_name = os.getenv("OTEL_SERVICE_NAME", "treadmill-worker")

    if not endpoint:
        _setup_noop()
        return

    from opentelemetry import metrics, trace
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor

    resource = Resource(attributes={SERVICE_NAME: service_name})

    _tracer_provider = TracerProvider(resource=resource)
    _tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
    )
    trace.set_tracer_provider(_tracer_provider)

    _meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=endpoint, insecure=True)
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
