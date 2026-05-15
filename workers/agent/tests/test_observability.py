"""Tests for treadmill_agent.observability.

Verifies the two-path behaviour: no-op when OTEL_EXPORTER_OTLP_ENDPOINT is
unset (the fully-local default), and provider registration when the endpoint
is configured.
"""

from __future__ import annotations

import logging
from unittest.mock import patch


def _fresh_module():
    """Reset module-level globals so each test starts clean."""
    import treadmill_agent.observability as mod
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

    logger = mod.get_logger("treadmill.agent.test")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "treadmill.agent.test"


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
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "treadmill-worker-test")
    mod = _fresh_module()

    with (
        patch("opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter"),
        patch("opentelemetry.exporter.otlp.proto.grpc.metric_exporter.OTLPMetricExporter"),
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
