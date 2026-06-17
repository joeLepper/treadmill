#!/usr/bin/env python3
"""CI secret-leak scan — fails the build if a PR ADDS a denylisted client token.

Server-side counterpart to the local ``pre_commit_secret_leak`` hook (which
only guards the operator's own commits). Reuses that module's pure core
(``added_lines`` + ``find_hits``).

Denylist source — OUT OF SOURCE CONTROL
---------------------------------------
GitHub-hosted runners can't read the operator-local
``~/.treadmill/codenames.json``, so the denylist is fed via the
``CODENAME_DENYLIST`` env var (newline-separated), wired in the workflow
from the ``TREADMILL_CODENAME_DENYLIST`` repo Actions secret. The literals
stay out of source. An EMPTY denylist (e.g. a fork PR — Actions does not
pass secrets to fork-triggered runs) → PASS (no-op): a fork contributor
can't leak client names they don't have, and the internal team's PRs carry
the secret.

PUBLIC-LOG SAFETY: Actions logs are public, so on a hit this prints only the
FILE + a COUNT — never the matched token value (that would re-leak the very
string we're blocking). Reads the unified diff on STDIN (the workflow pipes
``git diff --unified=0 <base>...HEAD``).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pre_commit_secret_leak import added_lines, find_hits  # noqa: E402


def main() -> int:
    denylist = [
        s.strip()
        for s in os.environ.get("CODENAME_DENYLIST", "").splitlines()
        if s.strip()
    ]
    if not denylist:
        print(
            "secret-leak scan: denylist empty (fork PR or unconfigured secret) "
            "— skipping",
            file=sys.stderr,
        )
        return 0

    hits = find_hits(added_lines(sys.stdin.read()), denylist)
    if not hits:
        print("secret-leak scan: clean ✓")
        return 0

    # REDACTED output — file + count only, never the token (public logs).
    files = sorted({path for path, _, _ in hits})
    print(
        "::error::secret-leak scan BLOCKED — this PR adds client-sensitive "
        "token(s). Codename them per the convention (operator: see "
        "~/.treadmill/codenames.json):",
        file=sys.stderr,
    )
    for f in files:
        n = len({tok for p, tok, _ in hits if p == f})
        print(f"::error::  {f} — {n} denylisted token(s)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
