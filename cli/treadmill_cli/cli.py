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

import re
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from treadmill_cli.api_client import ApiClient, ApiError
from treadmill_cli.commands.learnings import learnings_app
from treadmill_cli.commands.schedules import schedules_app
from treadmill_cli.config import load_config
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
    dev: Annotated[bool, typer.Option(
        "--dev",
        help=(
            "Local-only fast-path: when running against TREADMILL_LOCAL=true, "
            "an intent-only submission skips the wf-plan PR-merge gate and "
            "spawns an implicit one-task wf-author run immediately. Ignored "
            "in non-local environments."
        ),
    )] = False,
) -> None:
    """Submit a plan. Either ``--doc`` (Scenario 1) or ``--intent`` (Scenario 2)."""
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
            if dev:
                doc.write_text(doc_content, encoding="utf-8")

    try:
        with _client() as client:
            plan = client.create_plan(
                repo=repo,
                intent=intent,
                doc_path=doc_path,
                doc_content=doc_content,
                created_by=created_by,
                dev=dev,
            )
            console.print(f"[green]plan created:[/green] [bold]{plan['id']}[/bold]")
            if plan.get("intent"):
                console.print(f"  intent: {plan['intent']}")
            if plan.get("doc_path"):
                console.print(f"  doc:    {plan['doc_path']}")
            console.print(f"  repo:   {plan['repo']}")

            # Doc submissions always list spawned tasks; the --dev fast-path
            # also spawns an implicit task, so list those too.
            if doc_content is not None or (dev and intent is not None):
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


# ── submit (intent shorthand; auto-creates implicit one-task plan) ───────────


@app.command("submit")
def submit(
    intent: Annotated[str, typer.Argument(help="Free-text intent of the work.")],
    repo: Annotated[str, typer.Option("--repo", "-r", help="org/repo slug.")],
    workflow: Annotated[str, typer.Option(
        "--workflow", "-w", help="Workflow slug for the spawned task.",
    )] = "wf-author",
    created_by: Annotated[str | None, typer.Option("--created-by")] = None,
    dev: Annotated[bool, typer.Option(
        "--dev",
        help=(
            "Local-only fast-path: in TREADMILL_LOCAL=true environments, "
            "skips the wf-plan PR-merge gate and lets the API spawn the "
            "implicit wf-author task in the same transaction as the plan. "
            "Ignored in non-local environments."
        ),
    )] = False,
) -> None:
    """Submit a small change as an intent: creates an implicit one-task Plan
    plus a single Task under it. Per ADR-0010, every Task has a Plan; this
    command spares the user from authoring a plan doc for trivial work.

    With ``--dev`` (D.10), the API spawns the implicit one-task wf-author
    run itself in local mode — no follow-up POST /tasks is needed.
    """
    try:
        with _client() as client:
            plan = client.create_plan(
                repo=repo, intent=intent, created_by=created_by, dev=dev,
            )
            if dev:
                # API spawned the task implicitly; list to report it.
                tasks = client.list_plan_tasks(plan["id"])
                task = tasks[0] if tasks else None
            else:
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
