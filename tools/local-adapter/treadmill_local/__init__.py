"""Treadmill local adapter — interpret CDK synth output and run it on moto + Docker.

Public surface includes the runtime class itself plus the pytest-harness
seam (``wait_until_ready`` + the ``local_substrate`` fixture) used by
worker integration tests (Phase 4 B.12) and any other test that wants a
real substrate spun up programmatically.

The harness imports are deferred to the symbol-level so tools that
import ``treadmill_local`` without ``pytest`` installed (e.g. the CLI in
production) don't pay the import cost or hit a missing-dependency error.
"""

from treadmill_local.runtime import LocalRuntime

__version__ = "0.0.0"


def __getattr__(name: str):  # pragma: no cover — trivial lazy import
    """Lazy re-export of pytest-only symbols.

    ``wait_until_ready`` + ``local_substrate`` live in
    ``treadmill_local.pytest_harness`` which imports ``pytest`` at module
    load. The CLI doesn't need pytest, so importing it eagerly here would
    add a pointless dependency at runtime. ``__getattr__`` defers the
    import until someone actually asks for one of the names.
    """
    if name in ("wait_until_ready", "local_substrate", "SubstrateNotReadyError"):
        from treadmill_local.pytest_harness import (
            SubstrateNotReadyError,
            local_substrate,
            wait_until_ready,
        )
        return {
            "wait_until_ready": wait_until_ready,
            "local_substrate": local_substrate,
            "SubstrateNotReadyError": SubstrateNotReadyError,
        }[name]
    raise AttributeError(f"module 'treadmill_local' has no attribute {name!r}")


__all__ = [
    "LocalRuntime",
    "wait_until_ready",
    "local_substrate",
    "SubstrateNotReadyError",
]
