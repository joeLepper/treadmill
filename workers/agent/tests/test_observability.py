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
    mod._token_counters.clear()
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
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "treadmill-worker-test")
    mod = _fresh_module()

    # Mock out the instrumentors so no real instrumentation side effects fire.
    # Exporters are constructed for real to catch a regression where the
    # ``insecure=`` kwarg (gRPC-only) gets passed and raises TypeError.
    with (
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
    monkeypatch.setenv("OTEL_SERVICE_NAME", "treadmill-worker-test")
    mod = _fresh_module()

    with (
        patch(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter"
        ) as mock_span_exporter,
        patch(
            "opentelemetry.exporter.otlp.proto.http.metric_exporter.OTLPMetricExporter"
        ) as mock_metric_exporter,
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
    monkeypatch.setenv("OTEL_SERVICE_NAME", "treadmill-worker-test")
    mod = _fresh_module()

    with (
        patch(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter"
        ) as mock_span_exporter,
        patch(
            "opentelemetry.exporter.otlp.proto.http.metric_exporter.OTLPMetricExporter"
        ) as mock_metric_exporter,
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


# ── record_token_usage ────────────────────────────────────────────────────────


def test_record_token_usage_does_not_raise_with_noop_provider(monkeypatch):
    """record_token_usage succeeds silently when no endpoint is configured."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    mod = _fresh_module()
    mod.configure()

    mod.record_token_usage(
        model="claude-haiku-4-5-20251001",
        role="role-author",
        task_id="task-abc",
        step_id="step-xyz",
        input_tokens=100,
        output_tokens=30,
        cache_creation_tokens=5,
        cache_read_tokens=10,
    )
    # No assertion needed — we're verifying it doesn't raise.


def test_record_token_usage_creates_four_counters(monkeypatch):
    """The first call to record_token_usage populates _token_counters with
    the four expected instrument keys."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    mod = _fresh_module()
    mod.configure()

    assert mod._token_counters == {}

    mod.record_token_usage(
        model="claude-haiku-4-5-20251001",
        role="role-author",
        task_id="task-1",
        step_id="step-1",
        input_tokens=10,
        output_tokens=5,
        cache_creation_tokens=0,
        cache_read_tokens=0,
    )

    assert set(mod._token_counters) == {"input", "output", "cache_creation", "cache_read"}


def test_record_token_usage_reuses_counters_across_calls(monkeypatch):
    """Counter objects are created once and reused on subsequent calls
    (OTel instrument identity is stable within a MeterProvider lifetime)."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    mod = _fresh_module()
    mod.configure()

    mod.record_token_usage(
        model="m", role="r", task_id="t1", step_id="s1",
        input_tokens=1, output_tokens=1,
        cache_creation_tokens=0, cache_read_tokens=0,
    )
    counter_after_first = mod._token_counters["input"]

    mod.record_token_usage(
        model="m", role="r", task_id="t2", step_id="s2",
        input_tokens=2, output_tokens=2,
        cache_creation_tokens=0, cache_read_tokens=0,
    )
    assert mod._token_counters["input"] is counter_after_first


def test_record_token_usage_fresh_module_resets_counters(monkeypatch):
    """_fresh_module() clears _token_counters; new configure() triggers
    counter re-creation on the next record_token_usage call."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    mod = _fresh_module()
    mod.configure()

    mod.record_token_usage(
        model="m", role="r", task_id="t", step_id="s",
        input_tokens=1, output_tokens=1,
        cache_creation_tokens=0, cache_read_tokens=0,
    )
    assert "input" in mod._token_counters

    # Simulate module re-init (as happens between test isolation calls).
    _fresh_module()
    assert mod._token_counters == {}
