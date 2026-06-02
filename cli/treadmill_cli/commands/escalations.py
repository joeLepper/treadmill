"""Escalations command group — ``treadmill escalations [list|tail|close|ack|report]``.

Operator-level control over the incident lifecycle introduced in ADR-0062.
Each subcommand wraps a single endpoint on ``/api/v1/escalations``:

  * ``list``   — snapshot of open incidents (optional ``--reason``, ``--task``).
  * ``tail``   — long-poll over SSE for the open + ack + close stream.
  * ``close``  — emit ``escalation_closed`` with ``close_reason='operator_close'``.
  * ``ack``    — emit ``escalation_acknowledged``.
  * ``report`` — MTTR aggregation grouped by ``reason`` / ``day`` / ``task``.

The ``tail`` subcommand bootstraps with one ``list`` call so the operator
sees the current state on start, then connects to the SSE stream for
deltas. The standard "snapshot + delta" pattern keeps the CLI honest
without forcing the API to replay history on every reconnect.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

from treadmill_cli.api_client import ApiClient, ApiError
from treadmill_cli.config import load_config

escalations_app = typer.Typer(
    name="escalations",
    help="Escalation operations (ADR-0062: incident lifecycle + MTTR).",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)


def _client() -> ApiClient:
    return ApiClient(load_config())


def _handle_api_error(exc: ApiError) -> None:
    err_console.print(f"[red]error {exc.status_code}: {exc.detail}[/red]")
    raise typer.Exit(code=2)


def _fmt_dt(value: Any) -> str:
    """Truncate an ISO timestamp to ``YYYY-MM-DD HH:MM`` for the table.

    Accepts string (server-shaped) or datetime; returns ``"—"`` for
    falsy values so an absent timestamp renders cleanly.
    """
    if value in (None, ""):
        return "—"
    s = value if isinstance(value, str) else value.isoformat()
    # Slice covers both ``2026-06-02T14:00:00Z`` and ``2026-06-02T14:00:00+00:00``.
    return s[:16].replace("T", " ")


def _fmt_mttr(seconds: int) -> str:
    """Render seconds as ``HhMMm`` / ``MMm SSs`` / ``SSs`` depending on
    magnitude — operators read incident durations in time units, not raw
    seconds. ``0`` renders as ``"—"`` so empty buckets don't pretend to
    have data."""
    if seconds <= 0:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m{s:02d}s"
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h}h{m:02d}m"


# ── list ─────────────────────────────────────────────────────────────────────


@escalations_app.command("list")
def escalations_list(
    reason: Annotated[str | None, typer.Option(
        "--reason", "-r",
        help=(
            "Filter to escalations with this payload.reason "
            "(architect_cap / stuck_task_sweep / gate-broken)."
        ),
    )] = None,
    task: Annotated[str | None, typer.Option(
        "--task", "-t",
        help="Case-insensitive task_id prefix filter.",
    )] = None,
) -> None:
    """Snapshot the open escalation incidents."""
    params: dict[str, str] = {}
    if reason is not None:
        params["reason"] = reason
    if task is not None:
        params["task"] = task
    try:
        with _client() as client:
            rows = client._request("GET", "/api/v1/escalations", params=params)
    except ApiError as exc:
        _handle_api_error(exc)

    if not rows:
        console.print("[dim]no open escalations[/dim]")
        return

    table = Table(title=f"Open escalations ({len(rows)})")
    table.add_column("Task", style="dim")
    table.add_column("Repo")
    table.add_column("Reason")
    table.add_column("Opened (UTC)")
    table.add_column("Title")
    for row in rows:
        table.add_row(
            str(row["task_id"])[:8],
            row["repo"],
            row.get("reason") or "—",
            _fmt_dt(row.get("opened_at")),
            (row.get("title") or "")[:60],
        )
    console.print(table)


# ── tail ─────────────────────────────────────────────────────────────────────


@escalations_app.command("tail")
def escalations_tail(
    reason: Annotated[str | None, typer.Option(
        "--reason", "-r",
        help="Filter the bootstrap snapshot by payload.reason.",
    )] = None,
    task: Annotated[str | None, typer.Option(
        "--task", "-t",
        help="Filter the bootstrap snapshot by task_id prefix.",
    )] = None,
    timeout: Annotated[float, typer.Option(
        "--timeout",
        help=(
            "Read timeout on the SSE socket; relaxed (5 minutes) so "
            "idle streams don't drop while the operator is watching."
        ),
    )] = 300.0,
) -> None:
    """Long-poll the escalation lifecycle stream.

    Prints the current snapshot of open incidents (one row per task)
    then connects to the SSE feed and prints one line per
    ``escalated_to_operator`` / ``escalation_acknowledged`` /
    ``escalation_closed`` event as the server fans it out. Exit with
    Ctrl-C; the SSE handler is request-disconnect aware so the server
    side cleans up immediately.
    """
    config = load_config()

    # Snapshot first so the operator sees the current state on start.
    snapshot_params: dict[str, str] = {}
    if reason is not None:
        snapshot_params["reason"] = reason
    if task is not None:
        snapshot_params["task"] = task
    try:
        with _client() as client:
            rows = client._request(
                "GET", "/api/v1/escalations", params=snapshot_params,
            )
    except ApiError as exc:
        _handle_api_error(exc)

    if rows:
        console.print(f"[bold]Open escalations:[/bold] {len(rows)}")
        for row in rows:
            console.print(
                f"  [dim]{row['task_id'][:8]}[/dim] "
                f"{row['repo']} "
                f"[yellow]{row.get('reason') or '—'}[/yellow] "
                f"({_fmt_dt(row.get('opened_at'))})"
            )
    else:
        console.print("[dim]no open escalations at start[/dim]")
    console.print("[dim]— tailing /api/v1/escalations/stream (Ctrl-C to exit) —[/dim]")

    headers: dict[str, str] = {"Accept": "text/event-stream"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    try:
        with httpx.Client(
            base_url=config.api_url,
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=10.0),
        ) as stream_client:
            with stream_client.stream(
                "GET", "/api/v1/escalations/stream",
            ) as response:
                if response.status_code >= 400:
                    err_console.print(
                        f"[red]stream connect failed: {response.status_code}[/red]"
                    )
                    raise typer.Exit(code=2)
                for line in response.iter_lines():
                    if not line:
                        continue
                    # SSE lines starting with ``:`` are comments (the
                    # server's keepalive ticks); skip without printing.
                    if line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    raw = line[len("data:"):].strip()
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError:
                        err_console.print(f"[red]unparseable frame: {raw!r}[/red]")
                        continue
                    action = record.get("action", "?")
                    color = {
                        "escalated_to_operator": "red",
                        "escalation_acknowledged": "yellow",
                        "escalation_closed": "green",
                    }.get(action, "white")
                    console.print(
                        f"  [{color}]{action}[/{color}] "
                        f"task=[dim]{str(record.get('task_id'))[:8]}[/dim] "
                        f"ts={record.get('ts')}"
                    )
    except KeyboardInterrupt:
        console.print("[dim]— disconnected —[/dim]")
        raise typer.Exit(code=0)
    except httpx.HTTPError as exc:
        err_console.print(f"[red]stream error: {exc}[/red]")
        raise typer.Exit(code=2)


# ── close ────────────────────────────────────────────────────────────────────


@escalations_app.command("close")
def escalations_close(
    task_id: Annotated[str, typer.Argument(help="Task UUID with an open escalation.")],
) -> None:
    """Emit ``escalation_closed`` with ``close_reason='operator_close'``."""
    try:
        with _client() as client:
            resp = client._request(
                "POST", f"/api/v1/escalations/{task_id}/close",
            )
    except ApiError as exc:
        _handle_api_error(exc)
    console.print(
        f"[green]closed:[/green] task=[dim]{resp['task_id'][:8]}[/dim] "
        f"reason={resp['close_reason']} "
        f"mttr={_fmt_mttr(int(resp['mttr_seconds']))}"
    )


# ── ack ──────────────────────────────────────────────────────────────────────


@escalations_app.command("ack")
def escalations_ack(
    task_id: Annotated[str, typer.Argument(help="Task UUID with an open escalation.")],
) -> None:
    """Emit ``escalation_acknowledged`` for an open incident."""
    try:
        with _client() as client:
            resp = client._request(
                "POST", f"/api/v1/escalations/{task_id}/ack",
            )
    except ApiError as exc:
        _handle_api_error(exc)
    console.print(
        f"[yellow]acked:[/yellow] task=[dim]{resp['task_id'][:8]}[/dim] "
        f"event_id=[dim]{resp['event_id'][:8]}[/dim]"
    )


# ── report ───────────────────────────────────────────────────────────────────


@escalations_app.command("report")
def escalations_report(
    since: Annotated[str | None, typer.Option(
        "--since",
        help=(
            "ISO timestamp (e.g. 2026-06-01T00:00:00Z); defaults to "
            "7 days ago at midnight UTC."
        ),
    )] = None,
    by: Annotated[str, typer.Option(
        "--by",
        help="Group-by dimension: reason / day / task.",
    )] = "reason",
) -> None:
    """MTTR aggregation across closed escalation incidents."""
    if by not in ("reason", "day", "task"):
        err_console.print(
            f"[red]--by must be one of reason / day / task (got {by!r})[/red]"
        )
        raise typer.Exit(code=2)

    params: dict[str, str] = {"by": by}
    if since is not None:
        # Light client-side sanity — let the server be authoritative on
        # exact parsing; we just want to surface bad input fast.
        try:
            dt.datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            err_console.print(f"[red]--since is not a valid ISO timestamp: {since!r}[/red]")
            raise typer.Exit(code=2)
        params["since"] = since

    try:
        with _client() as client:
            resp = client._request(
                "GET", "/api/v1/escalations/report", params=params,
            )
    except ApiError as exc:
        _handle_api_error(exc)

    buckets = resp.get("buckets") or []
    total = resp.get("total", 0)
    since_at = resp.get("since")
    if not buckets:
        console.print(
            f"[dim]no closed incidents since {since_at} "
            f"(by={by})[/dim]"
        )
        return

    title = (
        f"MTTR report by {by} since "
        f"{_fmt_dt(since_at)} — {total} closed incident(s)"
    )
    key_header = {"reason": "Reason", "day": "Day", "task": "Task"}[by]
    table = Table(title=title)
    table.add_column(key_header)
    table.add_column("Count", justify="right")
    table.add_column("MTTR avg", justify="right")
    table.add_column("MTTR p50", justify="right")
    table.add_column("MTTR p95", justify="right")
    for bucket in buckets:
        key_value = bucket["key"]
        if by == "task":
            # Truncate task UUIDs to the same 8-char shape the other
            # tables use; reason / day keys are already short.
            key_value = str(key_value)[:8]
        table.add_row(
            key_value,
            str(bucket["count"]),
            _fmt_mttr(int(bucket["mttr_seconds_avg"])),
            _fmt_mttr(int(bucket["mttr_seconds_p50"])),
            _fmt_mttr(int(bucket["mttr_seconds_p95"])),
        )
    console.print(table)
