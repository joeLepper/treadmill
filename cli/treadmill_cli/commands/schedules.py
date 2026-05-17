"""Schedules command group — treadmill schedules [list|create|pause|resume|delete].

Operator-level control over the ADR-0035 schedule catalogue. Each subcommand
maps to a single API call on the /api/v1/schedules surface.
"""

from __future__ import annotations

import json
import os
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from treadmill_cli.api_client import ApiClient, ApiError
from treadmill_cli.config import load_config

schedules_app = typer.Typer(
    name="schedules",
    help="Schedule operations (ADR-0035: periodic workflow dispatch).",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)


def _client() -> ApiClient:
    return ApiClient(load_config())


def _handle_api_error(exc: ApiError) -> None:
    err_console.print(f"[red]error {exc.status_code}: {exc.detail}[/red]")
    raise typer.Exit(code=2)


def _fmt_dt(value: str | None) -> str:
    if value is None:
        return "—"
    return value[:16].replace("T", " ")


@schedules_app.command("list")
def schedules_list() -> None:
    """List active and paused schedules with next-fire time."""
    try:
        with _client() as client:
            schedules = client._request("GET", "/api/v1/schedules")
    except ApiError as exc:
        _handle_api_error(exc)

    if not schedules:
        console.print("[dim]no schedules configured[/dim]")
        return

    table = Table(title=f"Schedules ({len(schedules)})")
    table.add_column("ID", style="dim")
    table.add_column("Cron")
    table.add_column("Workflow")
    table.add_column("Status")
    table.add_column("Next Fire (UTC)")
    for s in schedules:
        color = "green" if s["status"] == "active" else "yellow"
        table.add_row(
            str(s["id"])[:8],
            s["cron_expression"],
            s["workflow_id"],
            f"[{color}]{s['status']}[/{color}]",
            _fmt_dt(s.get("next_fire_at")),
        )
    console.print(table)


@schedules_app.command("create")
def schedules_create(
    cron: Annotated[str, typer.Argument(help="5-field cron expression (e.g. '0 9 * * 1').")],
    workflow_id: Annotated[str, typer.Argument(help="Workflow slug to dispatch on each tick.")],
    jitter: Annotated[int, typer.Option(
        "--jitter", help="Jitter cap in seconds (default 60).",
    )] = 60,
    quiet_hours: Annotated[str | None, typer.Option(
        "--quiet-hours", help="Quiet window in HH-HH format (e.g. '20-6').",
    )] = None,
    quiet_tz: Annotated[str, typer.Option(
        "--quiet-tz", help="IANA timezone for quiet-hours evaluation.",
    )] = "America/Los_Angeles",
    payload: Annotated[str | None, typer.Option(
        "--payload", help="JSON object to use as the dispatch payload template.",
    )] = None,
    created_by: Annotated[str | None, typer.Option(
        "--created-by", help="Identifier of the operator creating this schedule.",
    )] = None,
) -> None:
    """Create a new active schedule that dispatches a workflow on a cron cadence."""
    if created_by is None:
        created_by = os.environ.get("USER") or "operator"

    payload_dict: dict[str, Any] = {}
    if payload is not None:
        try:
            payload_dict = json.loads(payload)
        except json.JSONDecodeError as exc:
            err_console.print(f"[red]--payload is not valid JSON: {exc}[/red]")
            raise typer.Exit(code=2)
        if not isinstance(payload_dict, dict):
            err_console.print("[red]--payload must be a JSON object (not array or scalar)[/red]")
            raise typer.Exit(code=2)

    body: dict[str, Any] = {
        "cron_expression": cron,
        "workflow_id": workflow_id,
        "jitter_seconds": jitter,
        "quiet_tz": quiet_tz,
        "payload_template": payload_dict,
        "created_by": created_by,
    }
    if quiet_hours is not None:
        body["quiet_hours"] = quiet_hours

    try:
        with _client() as client:
            s = client._request("POST", "/api/v1/schedules", json=body)
    except ApiError as exc:
        _handle_api_error(exc)

    console.print(f"[green]created:[/green] [bold]{s['id']}[/bold]")
    console.print(f"  cron:     {s['cron_expression']}")
    console.print(f"  workflow: {s['workflow_id']}")
    console.print(f"  status:   {s['status']}")
    if s.get("next_fire_at"):
        console.print(f"  next:     {_fmt_dt(s['next_fire_at'])}")


@schedules_app.command("pause")
def schedules_pause(
    schedule_id: Annotated[str, typer.Argument(help="Schedule UUID to pause.")],
) -> None:
    """Pause a schedule (stops new ticks without deleting it)."""
    try:
        with _client() as client:
            s = client._request(
                "PATCH",
                f"/api/v1/schedules/{schedule_id}",
                json={"status": "paused"},
            )
    except ApiError as exc:
        _handle_api_error(exc)
    console.print(f"[yellow]paused:[/yellow] [bold]{s['id']}[/bold]")


@schedules_app.command("resume")
def schedules_resume(
    schedule_id: Annotated[str, typer.Argument(help="Schedule UUID to resume.")],
) -> None:
    """Resume a paused schedule."""
    try:
        with _client() as client:
            s = client._request(
                "PATCH",
                f"/api/v1/schedules/{schedule_id}",
                json={"status": "active"},
            )
    except ApiError as exc:
        _handle_api_error(exc)
    console.print(f"[green]resumed:[/green] [bold]{s['id']}[/bold]")


@schedules_app.command("delete")
def schedules_delete(
    schedule_id: Annotated[str, typer.Argument(help="Schedule UUID to delete.")],
    yes: Annotated[bool, typer.Option(
        "--yes", "-y", help="Skip the confirmation prompt (for scripted use).",
    )] = False,
) -> None:
    """Permanently delete a schedule.

    Prompts for confirmation unless ``--yes`` is passed.
    """
    if not yes:
        confirmed = typer.confirm(
            f"Delete schedule {schedule_id}? This cannot be undone.",
            default=False,
        )
        if not confirmed:
            console.print("[yellow]aborted[/yellow]")
            raise typer.Exit(code=1)

    try:
        with _client() as client:
            client._request("DELETE", f"/api/v1/schedules/{schedule_id}")
    except ApiError as exc:
        _handle_api_error(exc)

    console.print(f"[red]deleted:[/red] [bold]{schedule_id}[/bold]")
