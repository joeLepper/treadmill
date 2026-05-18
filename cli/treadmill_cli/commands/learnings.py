"""Learnings command group — treadmill learnings crystallize.

Per ADR-0034, captured learnings are periodically crystallized into
rules + remediations. This command scans docs/learnings/*.md for
candidates and dispatches a single fan-out task to wf-crystallize-learning
(one task per CLI run; the workflow fans out internally per Q34.c).
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from treadmill_cli.api_client import ApiClient, ApiError
from treadmill_cli.config import load_config

learnings_app = typer.Typer(
    name="learnings",
    help="Learnings operations (ADR-0034: crystallization pipeline).",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)

_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
_SCALAR_LINE_RE = re.compile(r"^([a-z_]+):\s*(.+)$")

_CANDIDATE_KEYS = frozenset({"status", "crystallization_backoff_until"})


def _client() -> ApiClient:
    return ApiClient(load_config())


def _handle_api_error(exc: ApiError) -> None:
    err_console.print(f"[red]error {exc.status_code}: {exc.detail}[/red]")
    raise typer.Exit(code=2)


def _parse_frontmatter_scalars(text: str, keys: frozenset[str]) -> dict[str, str]:
    """Extract simple scalar values from YAML frontmatter for the requested keys."""
    m = _FRONT_MATTER_RE.match(text)
    if not m:
        return {}
    result: dict[str, str] = {}
    for line in m.group(1).splitlines():
        match = _SCALAR_LINE_RE.match(line)
        if match and match.group(1) in keys:
            result[match.group(1)] = match.group(2).strip()
    return result


def _is_candidate(path: Path, today: date | None = None) -> bool:
    """Return True if this learning is ready for crystallization.

    Criteria (ADR-0034):
    - status == "captured"
    - crystallization_backoff_until absent OR <= today
    """
    if today is None:
        today = date.today()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    fm = _parse_frontmatter_scalars(text, _CANDIDATE_KEYS)
    if fm.get("status") != "captured":
        return False
    backoff_until = fm.get("crystallization_backoff_until")
    if backoff_until:
        try:
            if date.fromisoformat(backoff_until[:10]) > today:
                return False
        except ValueError:
            pass
    return True


def scan_learnings(learnings_dir: Path, today: date | None = None) -> list[str]:
    """Return sorted list of candidate learning slugs (filename stems)."""
    if not learnings_dir.exists():
        return []
    return [
        path.stem
        for path in sorted(learnings_dir.glob("*.md"))
        if _is_candidate(path, today)
    ]


def _build_crystallize_doc(candidates: list[str]) -> str:
    """Generate a plan doc (``status: active``) for a crystallize run.

    Submitting via doc_content activates the plan immediately (no drafting
    phase) and lets the API spawn the wf-crystallize-learning task from
    the embedded sequence_of_work block.
    """
    n = len(candidates)
    slug_list = "\n".join(f"- {s}" for s in candidates)
    intent_lines = "\n".join(f"      - {s}" for s in candidates)
    plural = "s" if n != 1 else ""
    return f"""\
---
status: active
---

# Crystallize learnings ({n} candidate{plural})

{slug_list}

## sequence_of_work

```yaml
sequence_of_work:
  - id: crystallize-all
    title: "crystallize {n} learning(s)"
    workflow: wf-crystallize-learning
    intent: |
      Crystallize captured learnings:
{intent_lines}
    scope:
      files:
        - docs/learnings/*.md
        - docs/knowledge-base/rules/*.md
    validation:
      - kind: deterministic
        description: Crystallization task dispatched
        script: exit 0
```
"""


@learnings_app.command("crystallize")
def crystallize(
    repo: Annotated[str, typer.Option("--repo", "-r", help="org/repo slug.")],
    learnings_dir: Annotated[Path, typer.Option(
        "--learnings-dir",
        help="Directory containing learning markdown files.",
    )] = Path("docs/learnings"),
) -> None:
    """Scan captured learnings and dispatch a wf-crystallize-learning task.

    Finds all docs/learnings/*.md with status=captured and no active backoff,
    then submits a plan doc (status:active) whose sequence_of_work carries
    all candidate slugs. The plan is activated immediately (no drafting phase);
    the API spawns the task inline per Q34.c.
    """
    candidates = scan_learnings(learnings_dir)
    if not candidates:
        console.print("[yellow]no captured learnings ready for crystallization[/yellow]")
        return

    console.print(f"[bold]candidates ({len(candidates)}):[/bold]")
    for slug in candidates:
        console.print(f"  {slug}")

    doc_content = _build_crystallize_doc(candidates)

    try:
        with _client() as client:
            plan = client.create_plan(repo=repo, doc_content=doc_content)
            tasks = client.list_plan_tasks(plan["id"])
    except ApiError as exc:
        _handle_api_error(exc)

    task = tasks[0] if tasks else None
    console.print(
        f"[green]dispatched:[/green] plan=[bold]{plan['id']}[/bold]"
        + (f" task=[bold]{task['id']}[/bold]" if task else "")
    )
    console.print(f"  candidates: {len(candidates)}")
