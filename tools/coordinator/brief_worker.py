#!/usr/bin/env python3
"""Template a worker brief from task + plan + memory inputs (ADR-0084 Task 3B).

Used by the coordinator session: invoke as a subprocess, then relay
stdout to the target worker via cc-relay.py.

CLI:
  brief_worker.py --plan-id <id> --task-id <id> [--worker <label>]
                  [--team-dir <path>]
                  [--task-intent <text>]
                  [--task-scope <file1,file2,...>]
                  [--active-peers <label1,label2,...>]
                  [--related-adr <adr-number>]
                  [--gates <gate1,gate2,...>]

Inputs flow:
  --task-intent, --task-scope, --active-peers — supplied by the coordinator
    on the CLI; for v1 the coordinator gathers these from the task_board
    API + plan spec and passes them in. A future version may fetch them
    directly.
  --team-dir — defaults to ``$PWD`` (matching the coordinator workdir
    convention). Pitfalls are read from ``<team-dir>/memory/main.md``.

Output: a structured markdown brief on stdout, ready to relay via
cc-relay.py --file /dev/stdin (after pipe).

Smoke test target: ``python3 brief_worker.py --help`` exits 0; calling
with only --plan-id + --task-id emits a brief with placeholder text +
clear TODO markers so the coordinator can see what's missing.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

_MEMORY_PITFALLS_HEADING = re.compile(r"^##+\s*Pitfalls\s*$", re.IGNORECASE)
_MEMORY_NEXT_SECTION = re.compile(r"^##\s+")

# Defaults the coordinator should override; surfaced as TODO markers so
# the gap is visible in the brief text rather than hidden behind blank
# fields.
_PLACEHOLDER_INTENT = (
    "_TODO: coordinator must fill in task intent (one paragraph: why this "
    "task exists + what success looks like). Cite related ADR / plan._"
)
_PLACEHOLDER_SCOPE = (
    "_TODO: coordinator must list the scope files (every file the worker "
    "will create or modify, including the component AGENT.md and any "
    "existing test files for modules being modified)._"
)
_PLACEHOLDER_PEERS = (
    "_TODO: coordinator must list active peers so the worker knows whom "
    "to broadcast ownership claims to (cc-relay.py --to-many)._"
)


def _read_pitfalls(team_dir: Path, limit: int = 5) -> list[str]:
    """Extract up to ``limit`` pitfall entries from <team_dir>/memory/main.md.

    Pitfalls live under a ``## Pitfalls`` section as ``### YYYY-MM-DD ...``
    sub-entries. We pull the heading line of each entry; the full text
    stays in the file. Empty list if no memory file or no pitfalls section.
    """
    memory = team_dir / "memory" / "main.md"
    if not memory.exists():
        return []
    pitfalls: list[str] = []
    in_section = False
    for line in memory.read_text().splitlines():
        if _MEMORY_PITFALLS_HEADING.match(line):
            in_section = True
            continue
        if in_section and _MEMORY_NEXT_SECTION.match(line):
            break
        if in_section and line.startswith("### "):
            pitfalls.append(line[len("### "):].strip())
            if len(pitfalls) >= limit:
                break
    return pitfalls


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def build_brief(
    *,
    plan_id: str,
    task_id: str,
    worker: str | None,
    task_intent: str | None,
    task_scope: list[str],
    active_peers: list[str],
    pitfalls: list[str],
    related_adr: str | None,
    gates: list[str],
) -> str:
    """Compose the markdown brief from the resolved inputs."""
    target_line = f"**Worker**: `{worker}`" if worker else "**Worker**: _unassigned_"
    intent_section = task_intent if task_intent else _PLACEHOLDER_INTENT
    if related_adr and task_intent:
        intent_section = f"{intent_section}\n\nRelated: ADR-{related_adr}"
    elif related_adr:
        intent_section = f"{intent_section} (Related: ADR-{related_adr})"

    if task_scope:
        scope_section = "\n".join(f"- `{path}`" for path in task_scope)
    else:
        scope_section = _PLACEHOLDER_SCOPE

    if active_peers:
        peers_csv = ",".join(active_peers)
        peers_section = (
            f"Active peers: `{peers_csv}`.\n\n"
            f"Broadcast ownership claims via:\n"
            f"```\n"
            f'cc-relay.py --to-many "{peers_csv}" --subfolder worker \\\n'
            f"    --from <your-label> --type context \\\n"
            f'    "[from: <your-label>] Taking <file1>, <file2> for task '
            f'{task_id}. Don\'t touch those until I push."\n'
            f"```"
        )
    else:
        peers_section = _PLACEHOLDER_PEERS

    if pitfalls:
        pitfalls_section = "\n".join(f"- {item}" for item in pitfalls)
        pitfalls_section += (
            "\n\n_Full entries with WHY / How-to-apply in "
            "`~/.treadmill/teams/<slug>/memory/main.md`._"
        )
    else:
        pitfalls_section = (
            "_No pitfalls recorded yet for this repo; this is an early plan._"
        )

    if not gates:
        # Default gate set per ADR-0030 + plan-skill rules — these are the
        # rules that bounce PRs to feedback when missed, so surface them
        # in every brief.
        gates = [
            "docs-currency: any code module touched gets its `AGENT.md` updated "
            "(Key surfaces + Recent changes).",
            "existing tests: modules being modified must have their existing tests "
            "updated for any new dependency (loose mocks trip otherwise).",
            "deterministic validation: any `validation.script` you author must work "
            "in the worker sandbox (no `aws`, no `docker`, no live network — see "
            "feedback_verify_binaries_exist_in_sandbox.md).",
        ]
    gates_section = "\n".join(f"- {item}" for item in gates)

    # ADR-0086 Task G: the coordinator's PR-registration handler
    # (responsibility 3 in coordinator_prompt.md §12) parses the
    # orchestrator's reply for two REQUIRED lines:
    #
    #     PR: #<number>
    #     Branch: <branch-name>
    #
    # so it can call POST /api/v1/task_prs without round-tripping back
    # to the worker for clarification. The lines must land verbatim,
    # one per line, in the reply body — anywhere is fine but they MUST
    # be present once the PR is open. Without both lines the
    # coordinator falls back to a no-op + a relay back asking for the
    # missing piece.
    ack_block = f"""\
On receipt, reply with: `[from: <your-label>] Got it — working on {task_id}.`

When your PR is open, your reply MUST include these two lines exactly
(one per line, anywhere in the body):

```
PR: #<number>
Branch: <branch-name>
```

The coordinator's PR-registration handler (ADR-0086) parses both
lines and calls `POST /api/v1/task_prs` against the API. Without
both lines the task's PR is not registered + the coordinator will
relay back asking for the missing piece — saves a round trip."""

    return f"""\
# Task brief — `{task_id}`

{target_line}
**Plan**: `{plan_id}`

## Intent

{intent_section}

## Scope

{scope_section}

## Known pitfalls (from per-repo memory)

{pitfalls_section}

## Peers + ownership claims

{peers_section}

## Gates this PR must pass

{gates_section}

## Acknowledge

{ack_block}
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Template a worker brief for a coordinator-driven task "
        "(ADR-0084 Task 3B). Output to stdout; pipe into cc-relay.py.",
    )
    parser.add_argument("--plan-id", required=True, help="Plan UUID")
    parser.add_argument("--task-id", required=True, help="Task UUID")
    parser.add_argument("--worker", help="Target worker label")
    parser.add_argument(
        "--team-dir",
        default=os.environ.get("PWD", "."),
        help="Coordinator team directory (default: $PWD). Pitfalls are "
        "read from <team-dir>/memory/main.md.",
    )
    parser.add_argument(
        "--task-intent",
        help="One-paragraph task intent. Required for non-placeholder output.",
    )
    parser.add_argument(
        "--task-scope",
        help="Comma-separated list of files in scope (include AGENT.md + "
        "existing test files for modules being modified).",
    )
    parser.add_argument(
        "--active-peers",
        help="Comma-separated list of active worker labels on this plan.",
    )
    parser.add_argument(
        "--related-adr",
        help="ADR number to cite alongside the intent (e.g. 0084).",
    )
    parser.add_argument(
        "--gates",
        help="Comma-separated list of gate requirements. Defaults to the "
        "ADR-0030 + plan-skill set (docs-currency, existing tests, "
        "deterministic-validation sandbox).",
    )
    args = parser.parse_args(argv)

    team_dir = Path(args.team_dir).expanduser().resolve()
    pitfalls = _read_pitfalls(team_dir)

    brief = build_brief(
        plan_id=args.plan_id,
        task_id=args.task_id,
        worker=args.worker,
        task_intent=args.task_intent,
        task_scope=_split_csv(args.task_scope),
        active_peers=_split_csv(args.active_peers),
        pitfalls=pitfalls,
        related_adr=args.related_adr,
        gates=_split_csv(args.gates),
    )
    sys.stdout.write(brief)
    return 0


if __name__ == "__main__":
    sys.exit(main())
