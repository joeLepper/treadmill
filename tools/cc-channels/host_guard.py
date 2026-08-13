#!/usr/bin/env python3
"""Host-binding drift guard — pure decision module (ADR-0095, plan 3017bf41).

stdlib-only: no third-party deps. Safe to import from unit tests.

Usage (CLI):
    python3 host_guard.py check <label> <bound_host>

    bound_host: the host the API says the label is bound to; pass empty
    string to indicate unbound (no binding found in the API).

    Exit 0  → start allowed.
    Exit 1  → refused; reason printed to stderr.
    Exit 2  → bad arguments.
"""

from __future__ import annotations

import os
import sys


def refuse_reason(
    label: str, local_host: str | None, bound_host: str | None
) -> str | None:
    """Return None when this host may start label, else a refusal message.

    FAIL-CLOSED rules (ADR-0095):
    - local_host is None (TREADMILL_HOST unset) → refuse
    - bound_host is None (no binding in API)     → refuse
    - bound_host != local_host                   → refuse
    - bound_host == local_host                   → allow (return None)
    """
    if local_host is None:
        return (
            f"label={label!r}: TREADMILL_HOST is unset; "
            f"cannot verify host binding (ADR-0095)"
        )
    if bound_host is None:
        return f"label={label!r}: no host binding found; fail-closed (ADR-0095)"
    if bound_host != local_host:
        return (
            f"label={label!r}: bound to host {bound_host!r}, "
            f"this host is {local_host!r}; refusing to start (ADR-0095)"
        )
    return None


def local_host_name() -> str | None:
    """Return the TREADMILL_HOST env var value, or None when unset or empty.

    ADR-0097 governs host proof/auth; this function trusts the env var
    without cryptographic verification (out of scope for the drift guard).
    """
    return os.environ.get("TREADMILL_HOST") or None


if __name__ == "__main__":
    if len(sys.argv) < 4 or sys.argv[1] != "check":
        print("usage: host_guard.py check <label> <bound_host>", file=sys.stderr)
        print("  bound_host: empty string means unbound", file=sys.stderr)
        sys.exit(2)

    _label = sys.argv[2]
    _bound_raw = sys.argv[3]
    _bound_host: str | None = _bound_raw if _bound_raw else None

    _local = local_host_name()
    _reason = refuse_reason(_label, _local, _bound_host)
    if _reason is not None:
        print(_reason, file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
