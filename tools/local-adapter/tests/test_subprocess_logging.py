"""Tests for the bounded logging helpers used by adapter subprocesses.

Covers two properties the subprocesses depend on:

  - ``configure_rotating_logging`` actually rotates (the autoscaler /
    deploy-watcher log file cannot grow without bound).
  - ``RateLimitedErrorLogger`` logs the first occurrence of a given
    error in full and then collapses repeats — so a persistent failure
    no longer dumps a traceback every iteration.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from treadmill_local.subprocess_logging import (
    RateLimitedErrorLogger,
    configure_rotating_logging,
)


@pytest.fixture(autouse=True)
def _reset_root_logger() -> Iterator[None]:
    """Snapshot + restore the root logger so configure_rotating_logging's
    handler swaps don't bleed between tests."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)


# ── rotation ──────────────────────────────────────────────────────────────────


def test_configure_rotating_logging_rotates_when_max_bytes_exceeded(
    tmp_path: Path,
) -> None:
    """A tiny max_bytes + enough records produces a backup file and keeps
    the main file under the cap. That's the invariant the dev-local
    disk-fill incident needed: bounded total bytes, not unbounded append."""
    log_file = tmp_path / "logs" / "subproc.log"  # parent dir does not exist
    configure_rotating_logging(log_file, max_bytes=500, backups=3)

    logger = logging.getLogger("treadmill.test.rotation")
    # Each record formats to roughly 100+ bytes thanks to the timestamp
    # + level + logger-name prefix. 50 records well exceeds 500 bytes.
    for i in range(50):
        logger.info("filler record %03d %s", i, "x" * 40)

    # Flush handlers before inspecting file sizes.
    for h in logging.getLogger().handlers:
        h.flush()

    assert log_file.exists(), "main log file must exist after configure"
    assert log_file.stat().st_size <= 500, (
        f"rotation should keep main file ≤ max_bytes (got {log_file.stat().st_size})"
    )
    # At least one backup must have appeared — that's the rotation signal.
    backups = list(log_file.parent.glob("subproc.log.*"))
    assert backups, "expected at least one rotated backup file"


def test_configure_rotating_logging_creates_parent_dir(tmp_path: Path) -> None:
    """Callers pass paths under ``.treadmill-local/`` without pre-creating
    the dir. The helper must mkdir for them."""
    log_file = tmp_path / "deep" / "nested" / "out.log"
    configure_rotating_logging(log_file, max_bytes=10_000)
    logging.getLogger("treadmill.test.mkdir").info("hello")
    for h in logging.getLogger().handlers:
        h.flush()
    assert log_file.exists()


def test_configure_rotating_logging_replaces_existing_handlers(
    tmp_path: Path,
) -> None:
    """The subprocess might pick up a default stdout handler from a bare
    ``logger.info`` before configure runs. Configure must swap it out so
    we don't double-write into the parent's redirected stream (which the
    parent now sets to DEVNULL anyway, but the contract matters)."""
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    configure_rotating_logging(tmp_path / "out.log", max_bytes=10_000)
    assert sentinel not in root.handlers
    # Exactly one handler remains — the rotating one we installed.
    assert len(root.handlers) == 1


# ── rate-limited error logger ────────────────────────────────────────────────


class _MemHandler(logging.Handler):
    """A logging.Handler that just collects records for assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _named_logger(name: str) -> tuple[logging.Logger, _MemHandler]:
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    handler = _MemHandler()
    logger.addHandler(handler)
    return logger, handler


def test_rate_limited_logger_logs_first_in_full_then_summarizes() -> None:
    """200 repeats of the same exception type must produce exactly one
    full-traceback record plus a bounded set of summary records — far
    fewer than 200. That's the property that protects the log from a
    persistent error storm."""
    logger, handler = _named_logger("treadmill.test.ratelimit.basic")
    rl = RateLimitedErrorLogger(logger, summary_every=50)

    exc = ConnectionRefusedError("queue unreachable: 127.0.0.1:4566")
    for _ in range(200):
        rl.log(exc, "tick failed; continuing")

    full_traceback_records = [r for r in handler.records if r.exc_info is not None]
    assert len(full_traceback_records) == 1, (
        "first occurrence must log a full traceback exactly once"
    )

    # The remaining records are summaries — there must be far fewer than
    # the number of underlying repeats, and they must not carry exc_info
    # (no further tracebacks).
    summary_records = [r for r in handler.records if r.exc_info is None]
    assert summary_records, "expected at least one rolling summary"
    assert len(summary_records) < 10, (
        f"too many summaries: {len(summary_records)} (should bound far below 200)"
    )
    # Summaries name the exception type and count.
    summary_messages = [r.getMessage() for r in summary_records]
    assert any("ConnectionRefusedError" in m for m in summary_messages)
    assert any("consecutive" in m for m in summary_messages)


def test_rate_limited_logger_reset_re_arms_fresh_traceback() -> None:
    """After ``reset()``, the next failure — even one identical to the
    earlier burst — must log a fresh traceback. Without this the
    operator never sees a stack trace for a second incident of the same
    error type."""
    logger, handler = _named_logger("treadmill.test.ratelimit.reset")
    rl = RateLimitedErrorLogger(logger, summary_every=50)

    exc = RuntimeError("transient failure")
    rl.log(exc, "ctx")
    rl.log(exc, "ctx")  # repeat — summary path
    rl.reset()
    rl.log(exc, "ctx")  # post-reset — fresh traceback again

    full_traceback_records = [r for r in handler.records if r.exc_info is not None]
    assert len(full_traceback_records) == 2, (
        "reset() should re-arm a fresh traceback for the next failure"
    )


def test_rate_limited_logger_new_signature_re_arms_traceback() -> None:
    """A different exception type or message must log a fresh traceback
    even without a ``reset()`` — the signature changed, so this is a
    distinct failure mode."""
    logger, handler = _named_logger("treadmill.test.ratelimit.newsig")
    rl = RateLimitedErrorLogger(logger, summary_every=50)

    rl.log(ConnectionRefusedError("a"), "ctx")
    rl.log(TimeoutError("b"), "ctx")
    rl.log(ConnectionRefusedError("c"), "ctx")  # different message head

    full_traceback_records = [r for r in handler.records if r.exc_info is not None]
    assert len(full_traceback_records) == 3


def test_rate_limited_logger_summary_cadence_respects_summary_every() -> None:
    """With summary_every=5, the helper emits a summary on the 5th, 10th,
    15th repeat — three summaries across 15 total repeats (15 = 1 full
    + 14 repeats; the 5th, 10th, 15th occurrences trigger summaries)."""
    logger, handler = _named_logger("treadmill.test.ratelimit.cadence")
    rl = RateLimitedErrorLogger(logger, summary_every=5)

    exc = ValueError("same shape")
    for _ in range(15):
        rl.log(exc, "ctx")

    full = [r for r in handler.records if r.exc_info is not None]
    summaries = [r for r in handler.records if r.exc_info is None]
    assert len(full) == 1
    assert len(summaries) == 3


def test_rate_limited_logger_rejects_zero_summary_every() -> None:
    logger, _ = _named_logger("treadmill.test.ratelimit.bad")
    with pytest.raises(ValueError, match="summary_every must be >= 1"):
        RateLimitedErrorLogger(logger, summary_every=0)
