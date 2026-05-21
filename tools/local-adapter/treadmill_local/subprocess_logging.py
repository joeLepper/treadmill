"""Bounded logging for long-running adapter subprocesses.

Two concerns the autoscaler and deploy-watcher share:

  1. Their log files grow without bound. The parent ``treadmill-local up``
     opens ``.treadmill-local/<name>.log`` for append and redirects the
     subprocess's stdout/stderr into it; a dev-local session left running
     overnight filled a developer's disk on 2026-05-20.
  2. Their poll loops dump a full traceback every iteration when a
     persistent error (queue unreachable, credentials expired) keeps the
     ``except Exception`` arm hot. One real failure becomes thousands of
     stack-trace pages that drown out the actual signal in the log.

This module fixes both by giving the subprocess ownership of its own
rotating log file plus a small helper that logs the first occurrence of
a given exception signature in full and then collapses repeats into a
periodic counted summary.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path


def configure_rotating_logging(
    log_file: Path,
    *,
    level: int = logging.INFO,
    max_bytes: int = 10_000_000,
    backups: int = 3,
) -> None:
    """Configure the root logger to write to a size-rotating file.

    Replaces any handlers already attached to the root logger so that
    the default stdout handler the subprocess inherits via
    ``logging.basicConfig`` (or via a bare ``logger.info`` call before
    any explicit setup) does not duplicate every record into the
    parent's redirected stream.

    The parent directory is created if missing so callers can pass a
    path under ``.treadmill-local/`` without pre-creating the dir.
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backups,
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")
    )
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)


def _signature(exc: BaseException) -> str:
    """Stable per-error-shape key: type name + first line of str(exc).

    First line of ``str(exc)`` is usually the boto3 / urllib message
    head and stays constant for the same error condition; the tail
    (request IDs, retry counts) jitters. Coarse on purpose — we want
    "same failure mode" not "byte-identical exception".
    """
    message = str(exc).splitlines()[0] if str(exc) else ""
    return f"{type(exc).__name__}: {message}"


class RateLimitedErrorLogger:
    """Log the first instance of an error in full, then summarize repeats.

    The first occurrence of a given signature logs at ``ERROR`` with
    ``exc_info`` so operators get a full traceback. Subsequent
    occurrences of the same signature are counted; the helper emits a
    one-line ``WARNING`` summary at most once every ``summary_every``
    repeats so the log file does not balloon with stack traces while a
    persistent failure (e.g. credentials expired) keeps re-firing.

    Call ``reset()`` on a successful iteration so the next failure —
    even one with the same signature as the previous burst — logs a
    fresh traceback. This is the load-bearing detail: without
    ``reset()``, the loop could recover and then fail later with the
    same signature, and the operator would never see a traceback for
    the second incident.
    """

    def __init__(
        self,
        logger: logging.Logger,
        *,
        summary_every: int = 50,
    ) -> None:
        if summary_every < 1:
            raise ValueError(f"summary_every must be >= 1, got {summary_every}")
        self._logger = logger
        self._summary_every = summary_every
        self._current_signature: str | None = None
        self._consecutive: int = 0

    def log(self, exc: BaseException, context: str) -> None:
        """Record one occurrence of ``exc`` in the loop labeled ``context``."""
        signature = _signature(exc)
        if signature != self._current_signature:
            # New (or first) failure signature — log the full traceback.
            self._current_signature = signature
            self._consecutive = 1
            self._logger.error(
                "%s: %s", context, signature, exc_info=exc,
            )
            return
        self._consecutive += 1
        # Summarize at the configured cadence — emits on the Nth, 2Nth,
        # … occurrence so a wedged loop still pings the log periodically
        # without dumping a fresh traceback each time.
        if self._consecutive % self._summary_every == 0:
            self._logger.warning(
                "%s: %s still failing (%d consecutive)",
                context,
                type(exc).__name__,
                self._consecutive,
            )

    def reset(self) -> None:
        """Re-arm so the next failure logs a fresh traceback."""
        self._current_signature = None
        self._consecutive = 0


__all__ = [
    "RateLimitedErrorLogger",
    "configure_rotating_logging",
]
