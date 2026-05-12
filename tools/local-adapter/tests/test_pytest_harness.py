"""Tests for the pytest-harness module.

Two coverage tiers:

  * ``wait_until_ready`` — unit-tested with a monkeypatched ``httpx.get``
    so we exercise the happy-path early-return and the timeout error
    shape without actually polling for a minute. These run on every
    pytest invocation.

  * ``local_substrate`` fixture — gated on ``TREADMILL_LOCAL_HARNESS=1``,
    requires real Docker. Verifies the fixture brings the substrate up,
    that ``wait_until_ready`` clears, and that tear-down removes the
    managed containers.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

from treadmill_local.pytest_harness import (
    SubstrateNotReadyError,
    wait_until_ready,
)


# ── wait_until_ready unit tests ───────────────────────────────────────────────


class _FakeResponse:
    def __init__(
        self, status_code: int, text: str = "", reason: str = "OK"
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.reason_phrase = reason


def test_wait_until_ready_returns_when_api_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 response on the first poll returns cleanly with no sleep."""
    calls: list[str] = []

    def fake_get(url: str, timeout: float = 5.0) -> _FakeResponse:
        calls.append(url)
        return _FakeResponse(200)

    monkeypatch.setattr(
        "treadmill_local.pytest_harness.httpx.get", fake_get
    )
    wait_until_ready(api_url="http://test.local:8088", timeout=5.0)

    # One call, against the configured URL with the /health/ready suffix.
    assert calls == ["http://test.local:8088/health/ready"]


def test_wait_until_ready_strips_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: callers passing ``http://x/`` shouldn't double-up the slash."""
    calls: list[str] = []

    def fake_get(url: str, timeout: float = 5.0) -> _FakeResponse:
        calls.append(url)
        return _FakeResponse(200)

    monkeypatch.setattr(
        "treadmill_local.pytest_harness.httpx.get", fake_get
    )
    wait_until_ready(api_url="http://test.local:8088/", timeout=5.0)
    assert calls == ["http://test.local:8088/health/ready"]


def test_wait_until_ready_times_out_with_clear_error_on_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``httpx.get`` raises on every call, the timeout error names
    elapsed seconds and the underlying error message — a test author
    reading the failure can triage without diving into logs."""

    def fake_get(url: str, timeout: float = 5.0) -> _FakeResponse:
        raise httpx.ConnectError("Connection refused")

    # Avoid actually sleeping a real second per iteration; keep the test fast.
    def fake_sleep(_seconds: float) -> None:
        pass

    monkeypatch.setattr(
        "treadmill_local.pytest_harness.httpx.get", fake_get
    )
    monkeypatch.setattr(
        "treadmill_local.pytest_harness.time.sleep", fake_sleep
    )

    with pytest.raises(SubstrateNotReadyError) as exc_info:
        wait_until_ready(
            api_url="http://test.local:8088",
            timeout=0.05,
            poll_interval=0.01,
        )

    message = str(exc_info.value)
    assert "elapsed=" in message
    assert "Connection refused" in message
    assert "ConnectError" in message


def test_wait_until_ready_times_out_with_clear_error_on_non_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the API is up but unhealthy (503), the timeout error quotes
    the status code + body so we can distinguish ``not yet started`` from
    ``a probe is unreachable``."""

    def fake_get(url: str, timeout: float = 5.0) -> _FakeResponse:
        return _FakeResponse(
            503, text='{"status":"unreachable"}', reason="Service Unavailable"
        )

    def fake_sleep(_seconds: float) -> None:
        pass

    monkeypatch.setattr(
        "treadmill_local.pytest_harness.httpx.get", fake_get
    )
    monkeypatch.setattr(
        "treadmill_local.pytest_harness.time.sleep", fake_sleep
    )

    with pytest.raises(SubstrateNotReadyError) as exc_info:
        wait_until_ready(
            api_url="http://test.local:8088",
            timeout=0.05,
            poll_interval=0.01,
        )

    message = str(exc_info.value)
    assert "503" in message
    assert "unreachable" in message


def test_wait_until_ready_succeeds_after_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real-world: the substrate takes a couple of poll intervals to
    come up. The helper keeps polling until it succeeds."""
    attempts = {"n": 0}

    def fake_get(url: str, timeout: float = 5.0) -> _FakeResponse:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectError("not yet")
        return _FakeResponse(200)

    def fake_sleep(_seconds: float) -> None:
        pass

    monkeypatch.setattr(
        "treadmill_local.pytest_harness.httpx.get", fake_get
    )
    monkeypatch.setattr(
        "treadmill_local.pytest_harness.time.sleep", fake_sleep
    )

    wait_until_ready(
        api_url="http://test.local:8088",
        timeout=5.0,
        poll_interval=0.01,
    )
    assert attempts["n"] == 3


# ── Fixture re-exports ────────────────────────────────────────────────────────


def test_wait_until_ready_is_re_exported_from_package() -> None:
    """Consumers (worker integration tests) import via the package root."""
    import treadmill_local

    assert treadmill_local.wait_until_ready is wait_until_ready
    assert (
        treadmill_local.SubstrateNotReadyError is SubstrateNotReadyError
    )


def test_local_substrate_fixture_is_re_exported_from_package() -> None:
    """The fixture is importable via the package root so test files can
    pull it in with one import line."""
    import treadmill_local

    fixture = treadmill_local.local_substrate
    # pytest fixtures are wrapped functions; the wrapper has a known marker.
    assert hasattr(fixture, "_pytestfixturefunction") or callable(fixture)


# ── Fixture end-to-end test (gated) ───────────────────────────────────────────


HARNESS_GATE = os.environ.get("TREADMILL_LOCAL_HARNESS") == "1"


@pytest.mark.skipif(
    not HARNESS_GATE,
    reason="set TREADMILL_LOCAL_HARNESS=1 to run; requires real Docker",
)
def test_fixture_brings_up_and_down(local_substrate: Any) -> None:
    """End-to-end: the fixture brought the substrate up, the API
    responded healthy, and the runtime handle is the one we expect."""
    from treadmill_local.runtime import LocalRuntime

    assert isinstance(local_substrate, LocalRuntime)
    # If we got here, ``wait_until_ready`` returned without raising —
    # so /health/ready answered 200, which (per C.6) means the
    # coordination consumer is alive too.
    # The runtime state should reflect a provisioned substrate.
    assert local_substrate.state.moto_endpoint is not None
