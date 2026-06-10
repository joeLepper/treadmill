"""Treadmill CLI entrypoint.

Per ADR-0010, the CLI is the orchestrator's interface to the API. It wraps
HTTP calls to the Treadmill API and presents results via Rich tables for
human consumption.

Command groups:

  treadmill plan submit  --doc PATH | --intent TEXT  [--repo REPO]
  treadmill plan show    PLAN_ID
  treadmill plan list    [--repo REPO]
  treadmill submit       INTENT  [--repo REPO]   # auto-implicit one-task plan
  treadmill task show    TASK_ID
  treadmill task list    [--repo REPO] [--plan PLAN_ID] [--status STATUS]
  treadmill status       # API + dependencies
  treadmill observe ...  # read-only Grafana access layer (ADR-0020)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from treadmill_cli.api_client import ApiClient, ApiError
from treadmill_cli.commands.corpus import corpus_app
from treadmill_cli.commands.escalations import escalations_app
from treadmill_cli.commands.learnings import learnings_app
from treadmill_cli.commands.onboarding import onboarding_app
from treadmill_cli.commands.repo import repo_app
from treadmill_cli.commands.team import team_app
from treadmill_cli.commands.schedules import schedules_app
from treadmill_cli.config import load_config
from treadmill_cli.identity import resolve_created_by
from treadmill_cli.observe import observe_app


app = typer.Typer(
    name="treadmill",
    help="Treadmill CLI — submit plans, inspect tasks, manage runs.",
    no_args_is_help=True,
    add_completion=False,
)
plan_app = typer.Typer(name="plan", help="Plan operations.", no_args_is_help=True)
task_app = typer.Typer(name="task", help="Task operations.", no_args_is_help=True)
workflows_app = typer.Typer(
    name="workflows", help="Workflow operations.", no_args_is_help=True,
)
role_app = typer.Typer(
    name="role",
    help="Role operations (ADR-0028: DB-authoritative role prompts).",
    no_args_is_help=True,
)
app.add_typer(plan_app)
app.add_typer(task_app)
app.add_typer(workflows_app)
app.add_typer(role_app)
app.add_typer(learnings_app)
app.add_typer(observe_app)
app.add_typer(schedules_app)
app.add_typer(onboarding_app)
app.add_typer(team_app)
app.add_typer(repo_app)
app.add_typer(escalations_app)
app.add_typer(corpus_app)

console = Console()
err_console = Console(stderr=True)

# ── Plan doc status helpers ───────────────────────────────────────────────────

_FM_RE = re.compile(r"\A(---[ \t]*\n)(.*?)(\n---[ \t]*(?:\n|$))", re.DOTALL)
_FM_STATUS_RE = re.compile(r"^(status:[ \t]*)(\S+)", re.MULTILINE)
_TERMINAL_STATUSES = frozenset({"completed", "abandoned"})


def _promote_draft_status(content: str) -> str:
    """Flip ``status: drafting`` → ``status: active`` in plan doc frontmatter.

    Returns content unchanged if status is already ``active`` or absent.
    Raises ``ValueError(current_status)`` for terminal statuses so the
    caller can surface a human-readable error.
    """
    m = _FM_RE.match(content)
    if not m:
        return content
    fm_body = m.group(2)
    sm = _FM_STATUS_RE.search(fm_body)
    if not sm:
        return content
    status = sm.group(2)
    if status == "active":
        return content
    if status in _TERMINAL_STATUSES:
        raise ValueError(status)
    if status == "drafting":
        new_fm_body = fm_body[: sm.start(2)] + "active" + fm_body[sm.end(2):]
        return content[: m.start(2)] + new_fm_body + content[m.end(2):]
    return content


def _client() -> ApiClient:
    return ApiClient(load_config())


def _handle_api_error(exc: ApiError) -> None:
    err_console.print(f"[red]error {exc.status_code}: {exc.detail}[/red]")
    raise typer.Exit(code=2)


# ── plan submit ──────────────────────────────────────────────────────────────


@plan_app.command("submit")
def plan_submit(
    repo: Annotated[str, typer.Option("--repo", "-r", help="org/repo slug.")],
    doc: Annotated[Path | None, typer.Option(
        "--doc", "-d", help="Path to a plan markdown doc (Scenario 1).",
    )] = None,
    intent: Annotated[str | None, typer.Option(
        "--intent", "-i", help="Free-text intent (Scenario 2).",
    )] = None,
    created_by: Annotated[str | None, typer.Option(
        "--created-by", help="Identifier of the human or agent submitting.",
    )] = None,
) -> None:
    """Submit a plan. Either ``--doc`` (Scenario 1) or ``--intent`` (Scenario 2).

    Requires a team to be configured for the repo: run
    ``treadmill team up --repo <slug>`` first.
    """
    if doc is None and intent is None:
        err_console.print("[red]either --doc or --intent is required[/red]")
        raise typer.Exit(code=2)
    if doc is not None and intent is not None:
        err_console.print("[red]use only one of --doc or --intent[/red]")
        raise typer.Exit(code=2)

    doc_path: str | None = None
    doc_content: str | None = None
    if doc is not None:
        if not doc.exists():
            err_console.print(f"[red]doc file not found: {doc}[/red]")
            raise typer.Exit(code=2)
        doc_content = doc.read_text(encoding="utf-8")
        doc_path = str(doc)
        original_content = doc_content
        try:
            doc_content = _promote_draft_status(doc_content)
        except ValueError as exc:
            err_console.print(
                f"[red]cannot submit: plan doc status is '{exc}'; "
                f"only drafting or active docs may be submitted[/red]"
            )
            raise typer.Exit(code=2)
        if doc_content != original_content:
            console.print("  [dim]status: drafting → active[/dim]")

    try:
        with _client() as client:
            plan = client.create_plan(
                repo=repo,
                intent=intent,
                doc_path=doc_path,
                doc_content=doc_content,
                created_by=resolve_created_by(created_by),
            )
            console.print(f"[green]plan created:[/green] [bold]{plan['id']}[/bold]")
            if plan.get("intent"):
                console.print(f"  intent: {plan['intent']}")
            if plan.get("doc_path"):
                console.print(f"  doc:    {plan['doc_path']}")
            console.print(f"  repo:   {plan['repo']}")

            if doc_content is not None:
                tasks = client.list_plan_tasks(plan["id"])
                if tasks:
                    console.print(f"  tasks:  {len(tasks)} spawned")
    except ApiError as exc:
        _handle_api_error(exc)


# ── plan show ────────────────────────────────────────────────────────────────


@plan_app.command("show")
def plan_show(plan_id: str) -> None:
    """Show a plan and its tasks."""
    try:
        with _client() as client:
            plan = client.get_plan(plan_id)
            tasks = client.list_plan_tasks(plan_id)
    except ApiError as exc:
        _handle_api_error(exc)

    console.print(f"[bold]Plan {plan['id']}[/bold]")
    console.print(f"  repo:       {plan['repo']}")
    console.print(f"  intent:     {plan.get('intent') or '(none)'}")
    console.print(f"  doc_path:   {plan.get('doc_path') or '(none)'}")
    console.print(f"  created_by: {plan.get('created_by') or '(none)'}")
    console.print(f"  created_at: {plan.get('created_at') or '(none)'}")

    if not tasks:
        console.print("\n[dim]no tasks under this plan[/dim]")
        return
    table = Table(title=f"Tasks ({len(tasks)})")
    table.add_column("ID", style="dim")
    table.add_column("Title")
    table.add_column("Status")
    for task in tasks:
        table.add_row(
            str(task["id"])[:8],
            task["title"],
            task.get("derived_status") or "—",
        )
    console.print(table)


# ── plan list ────────────────────────────────────────────────────────────────


@plan_app.command("list")
def plan_list(
    repo: Annotated[str | None, typer.Option("--repo", "-r")] = None,
) -> None:
    """List recent plans (filtered by repo if --repo given).

    Note: at v0 the API does not yet have a list-plans endpoint; this is a
    follow-up. The command currently informs the user."""
    err_console.print(
        "[yellow]'plan list' is not yet implemented "
        "(API list-plans endpoint is a v0 follow-up).[/yellow]"
    )
    raise typer.Exit(code=1)


# ── plan validate ────────────────────────────────────────────────────────────


@plan_app.command("validate")
def plan_validate(
    doc: Annotated[Path, typer.Argument(help="Path to a plan markdown doc.")],
) -> None:
    """Validate a plan doc against the SKILL.md authoring rules.

    Catches sandbox-unsafe gates (live AWS, Docker, Postgres, package
    registry egress, dev-machine absolute paths) and format-brittle
    gates (exact-filename pins on timestamped artifacts, multi-arg
    call-signature greps) before the plan is dispatched. Exits 0 on
    clean, 1 on violations, 2 on parse errors.
    """
    if not doc.exists():
        err_console.print(f"[red]doc file not found: {doc}[/red]")
        raise typer.Exit(code=2)
    content = doc.read_text(encoding="utf-8")

    # Lazy-import: matches the existing pattern at line 456 — the CLI
    # shares the API parser via the uv workspace member install.
    from treadmill_api.parsers.plan_doc import PlanDocFormatError
    from treadmill_cli.plan_validate import validate_plan_doc

    try:
        violations = validate_plan_doc(content)
    except PlanDocFormatError as exc:
        err_console.print(f"[red]plan parse error: {exc}[/red]")
        raise typer.Exit(code=2) from exc
    except Exception as exc:  # pydantic.ValidationError, etc.
        err_console.print(f"[red]plan validation error: {exc}[/red]")
        raise typer.Exit(code=2) from exc

    # Post-mortem surprise B (2026-06-09 ADR-0085+0086): an ADR can
    # reference an HTTP endpoint that doesn't exist in the API surface
    # yet. The brief gets dispatched, the coordinator calls a 404,
    # the operator scrambles. Catch the gap here, surface as a warning
    # (NOT a violation) so the operator sees it before brief dispatch.
    # The check no-ops when the plan doesn't reference any ADR; runs
    # automatically when references are present.
    from treadmill_cli.adr_api_coverage import check_adr_api_coverage
    repo_root = _find_repo_root(doc)
    adr_gaps = (
        check_adr_api_coverage(content, repo_root=repo_root)
        if repo_root is not None else []
    )

    if not violations:
        console.print(f"[green]clean:[/green] {doc} ({len(content.splitlines())} lines)")
        if adr_gaps:
            _print_adr_coverage_warnings(adr_gaps)
        raise typer.Exit(code=0)

    table = Table(title=f"Plan-rule violations ({len(violations)})")
    table.add_column("Task", style="dim")
    table.add_column("Where")
    table.add_column("Rule", style="red")
    table.add_column("Detail")
    for v in violations:
        where = (
            f"validation[{v.validation_index}]"
            if v.validation_index is not None
            else "scope"
        )
        table.add_row(v.task_id, where, v.rule, v.detail)
    console.print(table)
    if adr_gaps:
        _print_adr_coverage_warnings(adr_gaps)
    err_console.print(
        "[red]plan failed validation; fix the gates above before "
        "dispatching (see citations in SKILL.md)[/red]"
    )
    raise typer.Exit(code=1)


def _find_repo_root(doc: Path) -> Path | None:
    """Walk up from the plan doc looking for the ``docs/adrs/`` +
    ``services/api/`` markers that identify the treadmill repo root.

    Returns the matching parent path or ``None`` if the doc is
    outside a treadmill checkout (in which case the coverage check
    is a no-op rather than an error)."""
    current = doc.resolve().parent
    while True:
        if (
            (current / "docs" / "adrs").is_dir()
            and (current / "services" / "api").is_dir()
        ):
            return current
        if current.parent == current:
            return None
        current = current.parent


def _print_adr_coverage_warnings(gaps: list) -> None:
    """Render ADR coverage gaps as plain warnings under the violations
    table. Warnings — not errors. Post-mortem surprise B framing:
    ADRs can legitimately reference future endpoints; the check
    surfaces gaps the operator should know about, doesn't block the
    plan from going out."""
    err_console.print(
        f"[yellow]ADR-referenced API coverage: {len(gaps)} gap(s)[/yellow]"
    )
    for gap in gaps:
        err_console.print(
            f"  [yellow]WARN[/yellow] {gap.adr_id} references "
            f"{gap.endpoint.method} {gap.endpoint.path} — "
            f"not found in route inventory"
        )


# ── plan check-migration-chain ───────────────────────────────────────────────


@plan_app.command("check-migration-chain")
def plan_check_migration_chain(
    versions_dir: Annotated[
        Path,
        typer.Option(
            "--versions-dir",
            help=(
                "Alembic versions directory to lint. Defaults to "
                "services/api/alembic/versions relative to the current "
                "working directory."
            ),
        ),
    ] = Path("services/api/alembic/versions"),
) -> None:
    """Lint the Alembic migration chain for branch collisions.

    Post-mortem surprise C from the ADR-0085+0086 plan: parallel migrations
    authored in the same dispatch window can both name the same
    ``down_revision``, branching the chain. This subcommand parses every
    migration file in the directory + builds the chain graph + reports
    multi-head collisions, duplicate revision ids, and dangling
    ``down_revision`` references. No DB connection required — runs in
    the worker sandbox.

    Exit codes: 0 clean; 1 violations found; 2 versions directory missing.
    """
    from treadmill_cli.migration_chain import find_chain_violations

    try:
        violations = find_chain_violations(versions_dir)
    except FileNotFoundError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    if not violations:
        console.print(
            f"[green]chain clean:[/green] {versions_dir} "
            f"({len([p for p in versions_dir.iterdir() if p.suffix == '.py']):d} files)"
        )
        raise typer.Exit(code=0)

    table = Table(title=f"Alembic chain violations ({len(violations)})")
    table.add_column("Kind", style="red")
    table.add_column("Revision(s)")
    table.add_column("File(s)", style="dim")
    table.add_column("Detail")
    for v in violations:
        table.add_row(
            v.kind,
            ", ".join(v.revisions),
            ", ".join(p.name for p in v.files),
            v.detail,
        )
    console.print(table)
    err_console.print(
        "[red]migration chain has violations; fix the down_revision "
        "of one of the conflicting migrations before merge[/red]"
    )
    raise typer.Exit(code=1)


# ── submit (intent shorthand; auto-creates implicit one-task plan) ───────────


@app.command("submit")
def submit(
    intent: Annotated[str, typer.Argument(help="Free-text intent of the work.")],
    repo: Annotated[str, typer.Option("--repo", "-r", help="org/repo slug.")],
    workflow: Annotated[str, typer.Option(
        "--workflow", "-w", help="Workflow slug for the spawned task.",
    )] = "wf-author",
    created_by: Annotated[str | None, typer.Option("--created-by")] = None,
) -> None:
    """Submit a small change as an intent: creates an implicit one-task Plan
    plus a single Task under it. Per ADR-0010, every Task has a Plan; this
    command spares the user from authoring a plan doc for trivial work.

    Requires a team to be configured for the repo: run
    ``treadmill team up --repo <slug>`` first.
    """
    try:
        with _client() as client:
            plan = client.create_plan(
                repo=repo, intent=intent, created_by=created_by,
            )
            task = client.create_task(
                plan_id=plan["id"], title=intent[:200],
                workflow=workflow, description=intent,
                created_by=created_by,
            )
            if task is not None:
                console.print(
                    f"[green]submitted:[/green] plan=[bold]{plan['id']}[/bold] "
                    f"task=[bold]{task['id']}[/bold]"
                )
                console.print(f"  status: {task.get('derived_status') or '—'}")
            else:
                console.print(
                    f"[green]submitted:[/green] plan=[bold]{plan['id']}[/bold]"
                )
    except ApiError as exc:
        _handle_api_error(exc)


# ── task show ────────────────────────────────────────────────────────────────


@task_app.command("show")
def task_show(task_id: str) -> None:
    try:
        with _client() as client:
            task = client.get_task(task_id)
    except ApiError as exc:
        _handle_api_error(exc)
    console.print(f"[bold]Task {task['id']}[/bold]")
    console.print(f"  plan_id:    {task['plan_id']}")
    console.print(f"  repo:       {task['repo']}")
    console.print(f"  title:      {task['title']}")
    console.print(f"  status:     {task.get('derived_status') or '—'}")
    console.print(f"  created_at: {task.get('created_at')}")
    if task.get("description"):
        console.print(f"\n  description:\n{task['description']}")


# ── task list ────────────────────────────────────────────────────────────────


@task_app.command("list")
def task_list(
    repo: Annotated[str | None, typer.Option("--repo", "-r")] = None,
    plan: Annotated[str | None, typer.Option("--plan", help="Plan ID filter.")] = None,
    status: Annotated[str | None, typer.Option("--status", help="derived_status filter.")] = None,
) -> None:
    try:
        with _client() as client:
            tasks = client.list_tasks(
                repo=repo, plan_id=plan, derived_status=status,
            )
    except ApiError as exc:
        _handle_api_error(exc)

    if not tasks:
        console.print("[dim]no tasks match the filters[/dim]")
        return
    table = Table(title=f"Tasks ({len(tasks)})")
    table.add_column("ID", style="dim")
    table.add_column("Repo")
    table.add_column("Title")
    table.add_column("Status")
    for task in tasks:
        table.add_row(
            str(task["id"])[:8],
            task["repo"],
            task["title"][:60],
            task.get("derived_status") or "—",
        )
    console.print(table)


# ── task retry ───────────────────────────────────────────────────────────────


@task_app.command("retry")
def task_retry(
    task_id: Annotated[str, typer.Argument(help="Task ID to retry.")],
    reason: Annotated[str, typer.Option(
        "--reason", "-r", help="One-line reason for the retry (required).",
    )],
    workflow: Annotated[str | None, typer.Option(
        "--workflow", "-w", help="Workflow slug (inferred if omitted).",
    )] = None,
    force_bypass_cap: Annotated[bool, typer.Option(
        "--force-bypass-cap",
        help="Bypass the per-workflow attempt cap.",
    )] = False,
) -> None:
    """Retry a task via POST /api/v1/tasks/{task-id}/retry."""
    try:
        with _client() as client:
            result = client.retry_task(
                task_id,
                reason,
                workflow=workflow,
                force_bypass_cap=force_bypass_cap,
            )
    except ApiError as exc:
        if exc.status_code == 409:
            err_console.print(f"[red]error: cap reached — {exc.detail}[/red]")
            err_console.print("[yellow]hint: pass --force-bypass-cap to override[/yellow]")
            raise typer.Exit(code=2)
        if exc.status_code == 404:
            err_console.print("[red]task not found[/red]")
            raise typer.Exit(code=2)
        _handle_api_error(exc)
    console.print(f"retry dispatched: workflow_run={result['workflow_run_id']}")


@task_app.command("note")
def task_note(
    task_id: Annotated[str, typer.Argument(help="Task ID.")],
    note: Annotated[str | None, typer.Argument(help="Note text to set.")] = None,
    clear: Annotated[bool, typer.Option(
        "--clear", help="Clear the note (set to null).",
    )] = False,
) -> None:
    """Set or clear the operator_note on a task (ADR-0081 §1).

    Usage:
      treadmill task note <task-id> "hint text here"
      treadmill task note <task-id> --clear
    """
    if clear:
        note_to_set = None
    elif note is None:
        err_console.print("[red]error: provide note text or pass --clear[/red]")
        raise typer.Exit(code=2)
    else:
        note_to_set = note

    try:
        with _client() as client:
            result = client.set_operator_note(task_id, note_to_set)
    except ApiError as exc:
        if exc.status_code == 404:
            err_console.print("[red]task not found[/red]")
            raise typer.Exit(code=2)
        _handle_api_error(exc)

    if note_to_set is None:
        console.print(f"[green]note cleared[/green] task={task_id}")
    else:
        console.print(
            f"[green]note set[/green] task={task_id}\n"
            f"excerpt: {note_to_set[:100]}"
            + ("..." if len(note_to_set) > 100 else "")
        )


# ── status ───────────────────────────────────────────────────────────────────


@app.command("status")
def status() -> None:
    """Check the API liveness + readiness."""
    try:
        with _client() as client:
            health = client.health()
            ready = client.ready()
    except ApiError as exc:
        _handle_api_error(exc)
    except Exception as exc:
        err_console.print(f"[red]could not reach API: {exc}[/red]")
        raise typer.Exit(code=2)

    console.print(f"[bold]Treadmill API[/bold]")
    console.print(f"  liveness:  [green]{health['status']}[/green] "
                  f"({health.get('service')} v{health.get('version')})")
    console.print(f"  readiness: [{'green' if ready['status'] == 'ok' else 'red'}]{ready['status']}[/]")
    checks = ready.get("checks") or {}
    if checks:
        for name, info in checks.items():
            color = "green" if info["status"] == "ok" else (
                "yellow" if info["status"] == "not_configured" else "red"
            )
            detail = f" ({info.get('detail')})" if info.get("detail") else ""
            console.print(f"    {name:<10} [{color}]{info['status']}[/]{detail}")


# ── workflows seed-starters ──────────────────────────────────────────────────


@workflows_app.command("seed-starters")
def workflows_seed_starters(
    reset_prompts_from_code: Annotated[bool, typer.Option(
        "--reset-prompts-from-code",
        help=(
            "Explicit recovery path (ADR-0028): for every role that "
            "already exists in the DB, PATCH its system_prompt back to "
            "the code-side definition. Off by default — the no-op 409 "
            "behavior is the normal idempotency. ONLY use this when "
            "the DB has diverged from what you expect and you want the "
            "bootstrap shape back."
        ),
    )] = False,
    yes: Annotated[bool, typer.Option(
        "--yes", "-y",
        help="Skip the confirmation prompt when --reset-prompts-from-code is set.",
    )] = False,
) -> None:
    """Seed the canonical seven starter workflows + their roles.

    Idempotent — a 409 on any role / workflow / version is treated as
    already-seeded and silently skipped, so re-running this command on
    a partially-seeded install heals the gap. Per decision #16 in the
    2026-05-11 closure plan.

    With ``--reset-prompts-from-code``, role 409s trigger a follow-up
    PATCH that overwrites the DB prompt with the code-side definition
    (ADR-0028 recovery path). Confirms interactively unless ``--yes``
    is passed for scripted use.
    """
    from treadmill_api.starters import STARTERS, StarterSeedError, seed

    if reset_prompts_from_code and not yes:
        confirm = typer.confirm(
            "--reset-prompts-from-code will overwrite existing role "
            "prompts in the DB with the code-side definition. This is "
            "destructive of any operator edits made via 'treadmill role "
            "update'. Continue?",
            default=False,
        )
        if not confirm:
            console.print("[yellow]aborted[/yellow]")
            raise typer.Exit(code=1)

    try:
        with _client() as client:
            result = seed(
                client, reset_prompts_from_code=reset_prompts_from_code,
            )
    except StarterSeedError as exc:
        err_console.print(f"[red]starter seed failed: {exc}[/red]")
        raise typer.Exit(code=2)
    except ApiError as exc:
        _handle_api_error(exc)

    total = len(STARTERS)
    console.print(
        f"[green]seeded:[/green] {result.fresh_workflows} new of {total} "
        f"starter workflows ({total - result.fresh_workflows} already existed)"
    )
    if result.role_prompts_reset:
        console.print(
            f"[yellow]reset prompts from code for "
            f"{len(result.role_prompts_reset)} role(s):[/yellow] "
            + ", ".join(result.role_prompts_reset)
        )


# ── workflows trigger (ADR-0053 Wave 3) ──────────────────────────────────────


@workflows_app.command("trigger")
def workflows_trigger(
    slug: Annotated[str, typer.Argument(help="Workflow slug to dispatch.")],
    payload: Annotated[str, typer.Option(
        "--payload",
        help="JSON object passed as the dispatch payload; must contain 'repo'.",
    )],
) -> None:
    """Trigger any workflow with a payload, independent of any task.

    Wraps ``POST /api/v1/workflows/{slug}/trigger``. Useful for an
    operator's first manual run of a workflow (e.g. the judge-prompt
    optimizer) or any future scheduled-bot dry-run.
    """
    try:
        parsed_payload = json.loads(payload)
    except json.JSONDecodeError as exc:
        err_console.print(f"[red]invalid --payload JSON: {exc}[/red]")
        raise typer.Exit(code=2)
    if not isinstance(parsed_payload, dict):
        err_console.print("[red]--payload must be a JSON object[/red]")
        raise typer.Exit(code=2)

    try:
        with _client() as client:
            result = client.trigger_workflow(slug, parsed_payload)
    except ApiError as exc:
        if exc.status_code == 404:
            err_console.print("[red]workflow not found[/red]")
            raise typer.Exit(code=2)
        if exc.status_code == 400:
            err_console.print(f"[red]{exc.detail}[/red]")
            raise typer.Exit(code=2)
        _handle_api_error(exc)
    console.print(f"triggered: workflow_run={result['run_id']}")


# ── role show / update / versions (ADR-0028) ─────────────────────────────────


@role_app.command("show")
def role_show(
    role_id: Annotated[str, typer.Argument(help="The role slug (e.g. role-reviewer).")],
    version: Annotated[int | None, typer.Option(
        "--version", "-v",
        help="A specific version to show. Omit to show the live prompt.",
    )] = None,
) -> None:
    """Print a role's current prompt + metadata, or a specific past version.

    Without ``--version``, hits ``GET /api/v1/roles/{id}`` and shows the
    live ``system_prompt``. With ``--version``, hits
    ``GET /api/v1/roles/{id}/versions/{version}`` and shows that
    snapshot including its notes + pr_url audit fields.
    """
    try:
        with _client() as client:
            if version is None:
                resp = client._request("GET", f"/api/v1/roles/{role_id}")
            else:
                resp = client._request(
                    "GET", f"/api/v1/roles/{role_id}/versions/{version}",
                )
    except ApiError as exc:
        _handle_api_error(exc)

    if version is None:
        console.print(
            f"[bold]{resp['id']}[/bold]  "
            f"model={resp['model']}  "
            f"kind={resp['output_kind']}"
        )
        console.print(f"[dim]updated_at: {resp['updated_at']}[/dim]")
        console.print()
        console.print(resp["system_prompt"])
    else:
        console.print(
            f"[bold]{role_id}[/bold]  v{resp['version']}  "
            f"created_at={resp['created_at']}  "
            f"by={resp.get('created_by') or 'unknown'}"
        )
        if resp.get("notes"):
            console.print(f"[dim]notes: {resp['notes']}[/dim]")
        if resp.get("pr_url"):
            console.print(f"[dim]pr_url: {resp['pr_url']}[/dim]")
        console.print()
        console.print(resp["system_prompt"])


@role_app.command("update")
def role_update(
    role_id: Annotated[str, typer.Argument(help="The role slug.")],
    prompt_from_file: Annotated[Path, typer.Option(
        "--prompt-from-file", "-f",
        help="Path to a file containing the new system_prompt.",
    )],
    notes: Annotated[str | None, typer.Option(
        "--notes", "-n",
        help="Optional rationale for the edit (audit trail).",
    )] = None,
    pr_url: Annotated[str | None, typer.Option(
        "--pr-url",
        help="Optional PR URL linking this edit to its review (audit trail).",
    )] = None,
) -> None:
    """PATCH a role's system_prompt and append a new role_versions row.

    Per ADR-0028, this is the supported edit path. Editing
    ``starters.py`` in code has NO effect on running deployments —
    the DB is authoritative for role prompts after bootstrap.
    """
    if not prompt_from_file.exists():
        err_console.print(f"[red]file not found: {prompt_from_file}[/red]")
        raise typer.Exit(code=2)
    new_prompt = prompt_from_file.read_text()
    if not new_prompt.strip():
        err_console.print(
            f"[red]file is empty: {prompt_from_file}[/red]"
        )
        raise typer.Exit(code=2)

    body: dict[str, Any] = {"system_prompt": new_prompt}
    if notes:
        body["notes"] = notes
    if pr_url:
        body["pr_url"] = pr_url

    try:
        with _client() as client:
            resp = client._request(
                "PATCH", f"/api/v1/roles/{role_id}", json=body,
            )
    except ApiError as exc:
        _handle_api_error(exc)

    console.print(
        f"[green]updated[/green] {role_id} → version {resp['version']}"
    )


@role_app.command("versions")
def role_versions(
    role_id: Annotated[str, typer.Argument(help="The role slug.")],
) -> None:
    """List a role's version history, newest first.

    Each row shows the version, created_at, created_by, notes, and
    pr_url. system_prompt is omitted for compactness — use
    ``role show <id> --version N`` to inspect a specific version's
    prompt content.
    """
    try:
        with _client() as client:
            versions = client._request(
                "GET", f"/api/v1/roles/{role_id}/versions",
            )
    except ApiError as exc:
        _handle_api_error(exc)

    if not versions:
        console.print(f"[yellow]no versions for {role_id}[/yellow]")
        return

    table = Table(title=f"versions: {role_id}", show_header=True)
    table.add_column("v", justify="right")
    table.add_column("created_at")
    table.add_column("created_by")
    table.add_column("notes")
    table.add_column("pr_url")
    for v in versions:
        table.add_row(
            str(v["version"]),
            v["created_at"],
            v.get("created_by") or "—",
            v.get("notes") or "—",
            v.get("pr_url") or "—",
        )
    console.print(table)


if __name__ == "__main__":
    app()
