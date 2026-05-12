"""Programmatic substrate lifecycle for pytest.

Two pieces:

  * ``wait_until_ready(api_url, timeout)`` — polls ``/health/ready`` until
    every wired probe reports OK. The coordination consumer's
    ``CoordinationProbe`` (C.6) is one of those probes, so a 200 here is
    a real signal that the substrate is ready to dispatch — not just
    that the FastAPI process accepted a connection.

  * ``local_substrate`` — session-scoped pytest fixture that brings the
    runtime up, calls ``wait_until_ready``, yields, and tears it down on
    session exit. Gated on the ``TREADMILL_LOCAL_HARNESS=1`` env var
    (distinct from ``TREADMILL_INTEGRATION``: the latter means "tests
    against an already-running substrate"; the former is "fixture brings
    it up itself"). Other tests can opt in by listing it in their
    fixture signature.

The harness is its own module so importing ``treadmill_local`` for
non-test use doesn't pull in pytest. The ``__init__.py`` re-export is
lazy via a separate import line — see that file's comment.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from treadmill_local.runtime import LocalRuntime


_DEFAULT_API_URL = "http://localhost:8088"
_DEFAULT_TIMEOUT = 60.0


class SubstrateNotReadyError(RuntimeError):
    """Raised when ``wait_until_ready`` exhausts its timeout.

    The error message names elapsed seconds and the last underlying
    error so a test author reading a failure has enough to triage
    without diving into the runtime logs.
    """


def wait_until_ready(
    api_url: str = _DEFAULT_API_URL,
    timeout: float = _DEFAULT_TIMEOUT,
    *,
    poll_interval: float = 1.0,
) -> None:
    """Block until ``GET {api_url}/health/ready`` returns 200, or raise.

    The probe checks for status==200; the readiness body's ``status``
    field is *also* "ok" iff every wired probe reports reachable, but
    the FastAPI route translates that into the HTTP status code already,
    so checking the code is sufficient and avoids parsing the body.

    A non-200 response or a connection error is retried on
    ``poll_interval`` until the deadline. The last error message is
    captured so the timeout exception can quote it.
    """
    deadline = time.monotonic() + timeout
    url = f"{api_url.rstrip('/')}/health/ready"
    last_error: str | None = None

    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=5.0)
            if response.status_code == 200:
                return
            last_error = (
                f"{response.status_code} {response.reason_phrase}: "
                f"{response.text[:200]}"
            )
        except httpx.HTTPError as exc:
            # Connection refused, DNS failure, read timeout, etc. — the
            # API isn't up yet (or has crashed). Keep polling; the
            # message is captured for the timeout error.
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(poll_interval)

    elapsed = timeout - max(deadline - time.monotonic(), 0.0)
    raise SubstrateNotReadyError(
        f"substrate did not become ready at {url} within {timeout:.1f}s "
        f"(elapsed={elapsed:.1f}s, last_error={last_error!r})"
    )


def _infra_dir() -> Path:
    """Locate the ``infra/`` directory the LocalRuntime synths from.

    The fixture is invoked from arbitrary CWDs (depending on which
    package is running pytest), so we walk up from this file to find
    the repo root and resolve from there.
    """
    here = Path(__file__).resolve()
    # tools/local-adapter/treadmill_local/pytest_harness.py
    # → repo root four parents up.
    repo_root = here.parents[3]
    infra = repo_root / "infra"
    if not infra.is_dir():
        raise RuntimeError(
            f"could not find infra/ relative to {here} "
            f"(walked up to {repo_root})"
        )
    return infra


@pytest.fixture(scope="session")
def local_substrate() -> Iterator[LocalRuntime]:
    """Session-scoped fixture that brings the substrate up + tears it down.

    Gated on ``TREADMILL_LOCAL_HARNESS=1``. Without the env var the
    fixture skips immediately — letting test files always request the
    fixture but only spend the docker time when the gate is open.

    The fixture is intentionally noisy on the way up (the runtime
    prints to its rich console) — pytest captures the output and shows
    it on test failures, which is the right tradeoff for a multi-minute
    setup that can fail in many places.
    """
    if os.environ.get("TREADMILL_LOCAL_HARNESS") != "1":
        pytest.skip(
            "set TREADMILL_LOCAL_HARNESS=1 to bring the substrate up via fixture"
        )

    runtime = LocalRuntime(_infra_dir())
    runtime.up()
    try:
        wait_until_ready()
        yield runtime
    finally:
        runtime.down()
