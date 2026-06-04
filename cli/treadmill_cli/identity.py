"""Session-aware identity resolution for CLI submit commands.

Per ADR-0068's identity scheme (label = bot name = state
dir = created_by), an orchestrator-in-residence's
submissions must carry the session label as
created_by, or the channel-server WS filter will not
match and the operator silently loses event visibility
on their own work. This module enforces that via a
single resolution function the submit commands call.
"""
from __future__ import annotations

import os
import sys
from typing import Final

SESSION_LABEL_ENV: Final = "TREADMILL_SESSION_LABEL"


def resolve_created_by(explicit: str | None) -> str:
    """Resolve the created_by value for a CLI submit.

    Rules (first match wins):
      1. explicit is None AND env var is set → env value.
      2. explicit is set AND matches env var → explicit (silent).
      3. explicit is set AND env var is set AND DIFFERS →
         explicit, but warn loud to stderr. Operator
         override is allowed but is a deviation worth
         surfacing.
      4. explicit is set AND env var is unset → explicit.
      5. explicit is None AND env var is unset →
         ``os.environ.get("USER") or "operator"`` (legacy
         three-tier fallback preserved).
    """
    session_label = os.environ.get(SESSION_LABEL_ENV) or None
    if explicit is None and session_label is not None:
        return session_label
    if explicit is not None and session_label is not None and explicit != session_label:
        print(
            f"warning: --created-by={explicit!r} disagrees with "
            f"{SESSION_LABEL_ENV}={session_label!r}; "
            f"using explicit value. Channel-server WS filter at "
            f"?created_by={session_label!r} will NOT match this "
            f"work; event visibility from this session may be silent.",
            file=sys.stderr,
        )
        return explicit
    if explicit is not None:
        return explicit
    # Neither explicit nor session-label-env: legacy fallback.
    return os.environ.get("USER") or "operator"
