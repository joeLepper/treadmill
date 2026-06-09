#!/usr/bin/env python3
"""Standalone CI helper for the ADR ↔ API-surface coverage check.

The same heuristic ``treadmill plan validate`` runs opportunistically
when a plan-doc references an ADR. This script lets CI run it
ad-hoc, e.g. over every plan in ``docs/plans/`` on every PR that
touches ``docs/adrs/`` or ``services/api/treadmill_api/routers/``.

Usage:
    tools/check-adr-api-coverage.py path/to/plan.md [more.md ...]
    tools/check-adr-api-coverage.py docs/plans/*.md

Exit codes:
    0 — no coverage gaps in any input plan doc
    1 — at least one plan doc has at least one gap
    2 — bad CLI usage (no plan-doc args)

Gaps are warnings — the check exists to surface "ADR said X, API
doesn't have X" before plan briefs go out. Adjust the exit code to
0 for everyone if your CI wants to keep the check informational
instead of blocking.
"""
from __future__ import annotations

import sys
from pathlib import Path

# This script lives at tools/check-adr-api-coverage.py; the repo root
# is the parent directory. Wire the cli/ src dir into sys.path so the
# import works without `uv pip install -e cli/`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "cli"))

from treadmill_cli.adr_api_coverage import check_adr_api_coverage  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(
            "usage: check-adr-api-coverage.py <plan.md> [more.md ...]",
            file=sys.stderr,
        )
        return 2

    total_gaps = 0
    for raw in args:
        plan_path = Path(raw)
        if not plan_path.exists():
            print(f"warn: plan doc not found: {plan_path}", file=sys.stderr)
            continue
        text = plan_path.read_text(encoding="utf-8")
        gaps = check_adr_api_coverage(text, repo_root=_REPO_ROOT)
        if not gaps:
            print(f"clean: {plan_path}")
            continue
        print(f"\n{plan_path} — {len(gaps)} ADR-coverage gap(s):")
        for gap in gaps:
            print(
                f"  WARN {gap.adr_id} references "
                f"{gap.endpoint.method} {gap.endpoint.path} — "
                f"not found in route inventory"
            )
        total_gaps += len(gaps)

    if total_gaps:
        print(
            f"\n{total_gaps} total gap(s) across {len(args)} plan doc(s).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
