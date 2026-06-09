#!/usr/bin/env python3
"""Coordinator handoff doc generator (ADR-0084 Task 3D).

Snapshots live task_board state for an assigned plan and writes a
handoff document that the incoming coordinator session reads on
startup. Pairs with the §2 startup reconciliation procedure in
coordinator_prompt.md.

CLI:
  handoff.py --plan-id <uuid> [--output-dir <path>] [--api-url <url>]

If --plan-id is not given, the script falls back to the first plan id
in ``TREADMILL_COORDINATOR_PLANS`` (CSV). The CLI is single-plan; a
coordinator handling multiple plans runs the script once per plan.

Inputs:
  GET <api_url>/api/v1/task_board/{plan_id}
  TREADMILL_API_URL env (default http://localhost:8088)
  TREADMILL_API_KEY env (Bearer; optional)
  TREADMILL_OPERATOR_INSTANCE env (optional; included in the doc when set)

Output:
  <output-dir>/handoff-<UTC-iso>.md  (default output-dir is $PWD,
    which is the team dir when run by a launched coordinator)
  prints the absolute path of the written file to stdout
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

_DEFAULT_API_URL = "http://localhost:8088"
_TIMEOUT_SECONDS = 10

# Statuses that signal an unresolved decision waiting on someone. The
# incoming coordinator should land on these first.
_BLOCKED_STATUSES = ("blocked_operator", "blocked_dependency")


def fetch_task_board(
    *, api_url: str, plan_id: str, api_key: str | None = None
) -> list[dict[str, Any]]:
    """Fetch the task_board snapshot for a plan from the Treadmill API.

    Raises ``urllib.error.HTTPError`` on non-200, ``ValueError`` on
    malformed JSON. Both surface as a non-zero CLI exit so the
    coordinator notices instead of writing a stale handoff doc.
    """
    url = f"{api_url.rstrip('/')}/api/v1/task_board/{plan_id}"
    req = urllib.request.Request(url)
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
        raw = resp.read().decode("utf-8")
    payload = json.loads(raw)
    if isinstance(payload, dict) and "rows" in payload:
        rows = payload["rows"]
    elif isinstance(payload, list):
        rows = payload
    else:
        raise ValueError(
            f"unexpected task_board payload shape: {type(payload).__name__}"
        )
    if not isinstance(rows, list):
        raise ValueError(f"task_board rows is not a list: {type(rows).__name__}")
    return rows


def _format_row(row: dict[str, Any]) -> str:
    def _cell(val: Any) -> str:
        if val is None or val == "":
            return "—"
        return str(val).replace("|", "\\|")

    task_id = _cell(row.get("task_id"))
    assignee = _cell(row.get("assignee"))
    status = _cell(row.get("status"))
    branch = _cell(row.get("branch"))
    pr = _cell(row.get("pr_number"))
    updated = _cell(row.get("updated_at"))
    return f"| `{task_id}` | {assignee} | {status} | {branch} | {pr} | {updated} |"


def _lane_summary(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_No tasks on the board for this plan._"
    by_assignee: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        assignee = row.get("assignee") or "_unassigned_"
        by_assignee[assignee].append(row)

    sections: list[str] = []
    for assignee in sorted(by_assignee):
        worker_rows = by_assignee[assignee]
        status_counts = Counter(r.get("status") or "_unknown_" for r in worker_rows)
        most_recent = max(
            (r.get("updated_at") for r in worker_rows if r.get("updated_at")),
            default=None,
        )
        status_line = ", ".join(
            f"{count} {status}" for status, count in sorted(status_counts.items())
        )
        sections.append(
            f"### `{assignee}`\n"
            f"- {len(worker_rows)} task(s): {status_line}\n"
            f"- Most recent updated_at: `{most_recent or '—'}`"
        )
    return "\n\n".join(sections)


def _unresolved_section(rows: list[dict[str, Any]]) -> str:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        status = row.get("status")
        if status in _BLOCKED_STATUSES:
            grouped[status].append(row)

    if not grouped:
        return "_No tasks currently blocked._"

    sections: list[str] = []
    for status in _BLOCKED_STATUSES:
        rows_in_status = grouped.get(status, [])
        if not rows_in_status:
            continue
        sections.append(f"### {status} ({len(rows_in_status)})")
        for row in rows_in_status:
            task_id = row.get("task_id") or "?"
            assignee = row.get("assignee") or "_unassigned_"
            notes = row.get("notes") or "_no notes recorded_"
            sections.append(
                f"- task `{task_id}` (assignee: `{assignee}`)\n"
                f"  - notes: {notes}"
            )
    return "\n".join(sections)


def build_handoff(
    *,
    plan_id: str,
    rows: list[dict[str, Any]],
    timestamp: str,
    api_url: str,
    operator_instance: str | None = None,
) -> str:
    """Compose the markdown handoff doc from resolved inputs. Pure function."""
    if rows:
        snapshot_lines = [
            "| task_id | assignee | status | branch | pr | updated_at |",
            "| --- | --- | --- | --- | --- | --- |",
            *(_format_row(r) for r in rows),
        ]
        snapshot_section = "\n".join(snapshot_lines)
    else:
        snapshot_section = "_No tasks on the board for this plan._"

    if operator_instance:
        operator_section = f"`{operator_instance}`"
    else:
        operator_section = (
            "_Not recorded in this handoff._ Set `TREADMILL_OPERATOR_INSTANCE` "
            "before generating the handoff, or recover the designation from "
            "the planning conversation."
        )

    return f"""\
# Coordinator handoff — plan `{plan_id}`

- **Generated:** `{timestamp}`
- **API source:** `{api_url}`
- **Operator instance:** {operator_section}

This document is a point-in-time snapshot. The incoming coordinator MUST
reconcile against a fresh `GET /api/v1/task_board/{{plan_id}}` before
acting — the live state may have moved since this file was written.

## Task board snapshot

{snapshot_section}

## Per-worker lane summary

{_lane_summary(rows)}

## Unresolved signals

{_unresolved_section(rows)}

## Recommended next actions for the incoming coordinator

1. Read `coordinator.env` and confirm `TREADMILL_COORDINATOR_PLANS` includes
   this plan id.
2. Re-fetch the task board (`GET /api/v1/task_board/{plan_id}`). Diff
   against this snapshot — any row whose `updated_at` is newer than this
   handoff's `Generated` timestamp moved during/after the handoff.
3. Brief any task in `ready` state that does not appear assigned in the
   live board.
4. For each blocked task in **Unresolved signals**, decide: resolve here,
   re-route to a peer, or escalate to the operator instance.
5. Update `task_board.updated_by` with your coordinator label as routing
   decisions land — this preserves the audit trail across the handoff.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a coordinator handoff doc from live task_board "
        "state (ADR-0084 Task 3D).",
    )
    parser.add_argument(
        "--plan-id",
        help="Plan UUID. If omitted, falls back to the first id in "
        "$TREADMILL_COORDINATOR_PLANS (CSV).",
    )
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("PWD", "."),
        help="Directory the handoff file is written to (default: $PWD, "
        "which is the team dir when launched by a coordinator session).",
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("TREADMILL_API_URL") or _DEFAULT_API_URL,
        help=f"Treadmill API base URL (default: $TREADMILL_API_URL or "
        f"{_DEFAULT_API_URL}).",
    )
    parser.add_argument(
        "--timestamp-override",
        help="UTC timestamp override (testing only; default: now()).",
    )
    args = parser.parse_args(argv)

    plan_id = args.plan_id
    if not plan_id:
        env_plans = (os.environ.get("TREADMILL_COORDINATOR_PLANS") or "").strip()
        if env_plans:
            plan_id = env_plans.split(",")[0].strip()
    if not plan_id:
        print(
            "error: --plan-id required (or set TREADMILL_COORDINATOR_PLANS)",
            file=sys.stderr,
        )
        return 2

    api_key = os.environ.get("TREADMILL_API_KEY") or os.environ.get(
        "BUNKHOUSE_API_KEY"
    )

    try:
        rows = fetch_task_board(
            api_url=args.api_url, plan_id=plan_id, api_key=api_key
        )
    except urllib.error.HTTPError as e:
        print(
            f"error: GET /api/v1/task_board/{plan_id} -> {e.code} {e.reason}",
            file=sys.stderr,
        )
        return 1
    except (urllib.error.URLError, ValueError) as e:
        print(f"error: fetch failed: {e}", file=sys.stderr)
        return 1

    if args.timestamp_override:
        timestamp = args.timestamp_override
    else:
        timestamp = (
            dt.datetime.now(dt.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )

    handoff = build_handoff(
        plan_id=plan_id,
        rows=rows,
        timestamp=timestamp,
        api_url=args.api_url,
        operator_instance=os.environ.get("TREADMILL_OPERATOR_INSTANCE"),
    )

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    # Filename collision-safe at second resolution under realistic usage;
    # if a coordinator triggers two handoffs in the same second, the second
    # overwrites the first (acceptable — they snapshot the same state).
    filename_ts = timestamp.replace(":", "").replace("-", "")
    out_path = output_dir / f"handoff-{filename_ts}.md"
    out_path.write_text(handoff)
    print(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
