"""Tests for treadmill_api.observability.

Verifies the two-path behaviour: no-op when OTEL_EXPORTER_OTLP_ENDPOINT is
unset (the fully-local default), and provider registration when the endpoint
is configured.
"""

from __future__ import annotations

import logging
from unittest.mock import patch


def _fresh_module():
    """Reload the observability module to reset module-level globals."""
    import treadmill_api.observability as mod
    # Reset globals so each test starts clean
    mod._tracer_provider = None
    mod._meter_provider = None
    mod._initialized = False
    return mod


def test_noop_when_endpoint_unset(monkeypatch):
    """configure() with no endpoint installs NoOp providers."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    mod = _fresh_module()
    mod.configure()

    from opentelemetry.trace import NoOpTracerProvider
    from opentelemetry.metrics import NoOpMeterProvider

    assert isinstance(mod._tracer_provider, NoOpTracerProvider)
    assert isinstance(mod._meter_provider, NoOpMeterProvider)


def test_get_tracer_returns_noop_tracer(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    mod = _fresh_module()
    mod.configure()

    tracer = mod.get_tracer("test")
    assert tracer is not None
    # NoOp tracer has start_as_current_span
    with tracer.start_as_current_span("test-span"):
        pass  # must not raise


def test_get_meter_returns_noop_meter(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    mod = _fresh_module()
    mod.configure()

    meter = mod.get_meter("test")
    assert meter is not None


def test_get_logger_returns_stdlib_logger(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    mod = _fresh_module()
    mod.configure()

    logger = mod.get_logger("treadmill.test")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "treadmill.test"


def test_configure_is_idempotent(monkeypatch):
    """Calling configure() twice doesn't reinitialize."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    mod = _fresh_module()
    mod.configure()
    provider_after_first = mod._tracer_provider
    mod.configure()
    assert mod._tracer_provider is provider_after_first


def test_real_providers_registered_when_endpoint_set(monkeypatch):
    """When OTEL_EXPORTER_OTLP_ENDPOINT is set, the SDK configures real
    TracerProvider / MeterProvider and registers them globally."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "treadmill-api-test")
    mod = _fresh_module()

    # Mock out the instrumentors so no real instrumentation side effects fire.
    # Exporters are constructed for real to catch a regression where the
    # ``insecure=`` kwarg (gRPC-only) gets passed and raises TypeError.
    with (
        patch("opentelemetry.instrumentation.fastapi.FastAPIInstrumentor"),
        patch("opentelemetry.instrumentation.sqlalchemy.SQLAlchemyInstrumentor"),
        patch("opentelemetry.instrumentation.httpx.HTTPXClientInstrumentor"),
        patch("opentelemetry.instrumentation.botocore.BotocoreInstrumentor"),
        patch("opentelemetry.instrumentation.logging.LoggingInstrumentor"),
        patch("opentelemetry.trace.set_tracer_provider") as mock_set_tracer,
        patch("opentelemetry.metrics.set_meter_provider") as mock_set_meter,
    ):
        mod.configure()

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.metrics import MeterProvider

    assert isinstance(mod._tracer_provider, TracerProvider)
    assert isinstance(mod._meter_provider, MeterProvider)
    # Global registration is the key correctness invariant.
    mock_set_tracer.assert_called_once_with(mod._tracer_provider)
    mock_set_meter.assert_called_once_with(mod._meter_provider)


def test_http_otlp_exporters_configured(monkeypatch):
    """ADR-0030: span + metric exporters must be the HTTP/protobuf variants
    pointed at per-signal paths (``/v1/traces``, ``/v1/metrics``) on :4318.
    The gRPC exporter cannot speak to the collector's HTTP port, so a naive
    import would silently lose all telemetry. The HTTP exporter also rejects
    the gRPC-only ``insecure=`` kwarg with TypeError — configure() must not
    pass it."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "treadmill-api-test")
    mod = _fresh_module()

    with (
        patch(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter"
        ) as mock_span_exporter,
        patch(
            "opentelemetry.exporter.otlp.proto.http.metric_exporter.OTLPMetricExporter"
        ) as mock_metric_exporter,
        patch("opentelemetry.instrumentation.fastapi.FastAPIInstrumentor"),
        patch("opentelemetry.instrumentation.sqlalchemy.SQLAlchemyInstrumentor"),
        patch("opentelemetry.instrumentation.httpx.HTTPXClientInstrumentor"),
        patch("opentelemetry.instrumentation.botocore.BotocoreInstrumentor"),
        patch("opentelemetry.instrumentation.logging.LoggingInstrumentor"),
        patch("opentelemetry.trace.set_tracer_provider"),
        patch("opentelemetry.metrics.set_meter_provider"),
    ):
        mod.configure()

        # Patch target = HTTP module path; the call landing here is what
        # proves configure() imported the HTTP exporter rather than gRPC.
        # No ``insecure=`` kwarg — that's gRPC-only and raises TypeError
        # against the HTTP exporter.
        mock_span_exporter.assert_called_once_with(
            endpoint="http://localhost:4318/v1/traces"
        )
        mock_metric_exporter.assert_called_once_with(
            endpoint="http://localhost:4318/v1/metrics"
        )


def test_endpoint_trailing_slash_is_normalized(monkeypatch):
    """Trailing slash on OTEL_EXPORTER_OTLP_ENDPOINT must not produce a
    double-slash signal path (``//v1/traces``) that the collector rejects."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318/")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "treadmill-api-test")
    mod = _fresh_module()

    with (
        patch(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter"
        ) as mock_span_exporter,
        patch(
            "opentelemetry.exporter.otlp.proto.http.metric_exporter.OTLPMetricExporter"
        ) as mock_metric_exporter,
        patch("opentelemetry.instrumentation.fastapi.FastAPIInstrumentor"),
        patch("opentelemetry.instrumentation.sqlalchemy.SQLAlchemyInstrumentor"),
        patch("opentelemetry.instrumentation.httpx.HTTPXClientInstrumentor"),
        patch("opentelemetry.instrumentation.botocore.BotocoreInstrumentor"),
        patch("opentelemetry.instrumentation.logging.LoggingInstrumentor"),
        patch("opentelemetry.trace.set_tracer_provider"),
        patch("opentelemetry.metrics.set_meter_provider"),
    ):
        mod.configure()

        mock_span_exporter.assert_called_once_with(
            endpoint="http://localhost:4318/v1/traces"
        )
        mock_metric_exporter.assert_called_once_with(
            endpoint="http://localhost:4318/v1/metrics"
        )
