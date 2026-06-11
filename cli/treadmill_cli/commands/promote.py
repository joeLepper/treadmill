"""Promote command group — treadmill promote [list|show|approve|reject].

The operator surface of the ADR-0088 prod-promotion human gate. The CLI is
the SINGLE WRITE PATH for promotion decisions (Telegram is a lens only):
``approve``/``reject`` send ``X-Operator-Key`` from ``TREADMILL_OPERATOR_KEY``
in the operator's environment — the key exists nowhere else, which is the
structural enforcement (coordinator/worker sessions cannot approve).

``approve`` does two things in order (ADR-0088 §3): records the decision
(keyed API call), then fires the repo's ``promote-to-prod.yml`` via
``gh workflow run`` with the proposal_id. The API holds no GitHub
credentials; the workflow re-verifies the proposal before deploying, so
the dispatcher is untrusted by design. If the dispatch step fails,
re-running ``approve`` is safe — the endpoint is idempotent on an
already-approved proposal and the workflow's started-transition CAS
prevents double-deploys.
"""

from __future__ import annotations

import os
import subprocess
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from treadmill_cli.api_client import ApiClient, ApiError
from treadmill_cli.config import load_config

promote_app = typer.Typer(
    name="promote",
    help="Prod-promotion human gate (ADR-0088): list, inspect, approve, reject.",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)

PROMOTE_WORKFLOW = "promote-to-prod.yml"


def _client() -> ApiClient:
    return ApiClient(load_config())


def _handle_api_error(exc: ApiError) -> None:
    err_console.print(f"[red]error {exc.status_code}: {exc.detail}[/red]")
    raise typer.Exit(code=2)


def _operator_key() -> str:
    key = os.environ.get("TREADMILL_OPERATOR_KEY", "").strip()
    if not key:
        err_console.print(
            "[red]TREADMILL_OPERATOR_KEY is not set in this shell.[/red]\n"
            "Promotion decisions are operator-only; the key lives in the "
            "operator's shell profile and nowhere else (ADR-0088 §2)."
        )
        raise typer.Exit(code=2)
    return key


def _fmt_dt(value: str | None) -> str:
    if value is None:
        return "—"
    return value[:16].replace("T", " ")


@promote_app.command("list")
def promote_list(
    repo: Annotated[
        str | None, typer.Option(help="Filter to one repository.")
    ] = None,
) -> None:
    """Pending + recent proposals, newest first."""
    try:
        with _client() as client:
            params = f"?repo={repo}" if repo else ""
            rows = client._request("GET", f"/api/v1/prod_promotions{params}")
    except ApiError as exc:
        _handle_api_error(exc)
    table = Table(title="prod promotions")
    for col in ("proposal_id", "repo", "status", "proposed", "expires", "decided_by"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            r["proposal_id"],
            r["repo"],
            r["status"],
            _fmt_dt(r.get("created_at")),
            _fmt_dt(r.get("expires_at")),
            r.get("decided_by") or "—",
        )
    console.print(table)


@promote_app.command("show")
def promote_show(proposal_id: str) -> None:
    """Full bundle for one proposal — the diff_summary is what you approve."""
    try:
        with _client() as client:
            row = client._request(
                "GET", f"/api/v1/prod_promotions/{proposal_id}"
            )
    except ApiError as exc:
        _handle_api_error(exc)
    bundle: dict[str, Any] = row.get("bundle", {})
    console.print(
        f"[bold]{row['proposal_id']}[/bold]  {row['repo']}  "
        f"status=[bold]{row['status']}[/bold]"
    )
    console.print(
        f"window: {bundle.get('env_from')} → {bundle.get('env_to')}   "
        f"expires: {_fmt_dt(row.get('expires_at'))}   "
        f"proposed_by: {bundle.get('proposed_by')}"
    )
    evidence = bundle.get("staging_evidence", {})
    console.print(
        f"staging evidence: sha={evidence.get('sha', '?')[:12]} "
        f"smoke_passed_at={evidence.get('smoke_passed_at', '?')}"
    )
    console.print(f"diff anchor: {bundle.get('diff_anchor', '?')}")
    console.print("[bold]diff summary (what you are approving):[/bold]")
    for line in bundle.get("diff_summary", []):
        console.print(f"  • {line}")
    digests = Table(title="digests (pinned — deployed exactly as listed)")
    digests.add_column("service")
    digests.add_column("digest")
    for d in bundle.get("digests", []):
        digests.add_row(d.get("service", "?"), d.get("digest", "?"))
    console.print(digests)
    if row.get("decided_by"):
        console.print(
            f"decision: {row['status']} by {row['decided_by']} "
            f"at {_fmt_dt(row.get('decided_at'))}"
            + (f" — {row['decision_note']}" if row.get("decision_note") else "")
        )


def _dispatch_workflow(repo: str, proposal_id: str) -> bool:
    """Fire promote-to-prod.yml via gh. Returns True on accepted dispatch."""
    result = subprocess.run(
        [
            "gh", "workflow", "run", PROMOTE_WORKFLOW,
            "--repo", repo,
            "-f", f"proposal_id={proposal_id}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        err_console.print(
            f"[yellow]workflow dispatch failed:[/yellow] {result.stderr.strip()}\n"
            "The approval IS recorded. Re-run this command to retry the "
            "dispatch — approve is idempotent and the workflow's single-use "
            "check prevents double-deploys."
        )
        return False
    return True


@promote_app.command("approve")
def promote_approve(
    proposal_id: str,
    note: Annotated[str | None, typer.Option(help="Optional decision note.")] = None,
    decided_by: Annotated[
        str, typer.Option(help="Recorded as the deciding operator.")
    ] = "joe",
    no_dispatch: Annotated[
        bool,
        typer.Option(
            "--no-dispatch",
            help="Record the approval without firing the workflow.",
        ),
    ] = False,
) -> None:
    """Approve a proposal (operator-keyed), then fire promote-to-prod."""
    key = _operator_key()
    try:
        with _client() as client:
            row = client._request(
                "POST",
                f"/api/v1/prod_promotions/{proposal_id}/approve",
                json={"decided_by": decided_by, "note": note},
                headers={"X-Operator-Key": key},
            )
    except ApiError as exc:
        _handle_api_error(exc)
    console.print(
        f"[green]approved[/green] {row['proposal_id']} ({row['repo']}) "
        f"by {row['decided_by']}"
    )
    if no_dispatch:
        console.print("dispatch skipped (--no-dispatch).")
        return
    if _dispatch_workflow(row["repo"], row["proposal_id"]):
        console.print(
            f"[green]dispatched[/green] {PROMOTE_WORKFLOW} — the workflow "
            "re-verifies the proposal before deploying."
        )
    else:
        raise typer.Exit(code=3)


@promote_app.command("reject")
def promote_reject(
    proposal_id: str,
    reason: Annotated[str, typer.Option(help="Required rejection reason.")],
    decided_by: Annotated[
        str, typer.Option(help="Recorded as the deciding operator.")
    ] = "joe",
) -> None:
    """Reject a proposal (operator-keyed)."""
    key = _operator_key()
    try:
        with _client() as client:
            row = client._request(
                "POST",
                f"/api/v1/prod_promotions/{proposal_id}/reject",
                json={"decided_by": decided_by, "reason": reason},
                headers={"X-Operator-Key": key},
            )
    except ApiError as exc:
        _handle_api_error(exc)
    console.print(
        f"[red]rejected[/red] {row['proposal_id']} ({row['repo']}): {reason}"
    )
