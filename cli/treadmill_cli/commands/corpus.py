"""Corpus command group — ``treadmill corpus export ...``.

Per ADR-0070 substep 3 task 4, materializes labeled gold rows into
JSONL via the API endpoint. The CLI is HTTP-only (ADR-0010) — it does
NOT open a DB session; it POSTs to
``/api/v1/dashboard/corpus/<kind>/export`` with the operator-supplied
``out_path`` and prints the row count.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console

from treadmill_cli.api_client import ApiClient, ApiError
from treadmill_cli.config import load_config

corpus_app = typer.Typer(
    name="corpus",
    help="Corpus export operations (ADR-0070 substep 3 task 4).",
    no_args_is_help=True,
)

export_app = typer.Typer(
    name="export",
    help="Materialize labeled gold rows into JSONL.",
    no_args_is_help=True,
)
corpus_app.add_typer(export_app)

console = Console()
err_console = Console(stderr=True)


def _client() -> ApiClient:
    return ApiClient(load_config())


def _handle_api_error(exc: ApiError) -> None:
    err_console.print(f"[red]error {exc.status_code}: {exc.detail}[/red]")
    raise typer.Exit(code=2)


@export_app.command("architect-gold")
def architect_gold(
    out: Annotated[
        str,
        typer.Option(
            "--out", "-o",
            help="Path to write the JSONL corpus to.",
        ),
    ],
) -> None:
    """Export labeled architect-gold rows to <out> as JSONL."""
    with _client() as client:
        try:
            response = client._request(
                "POST",
                "/api/v1/dashboard/corpus/architect-gold/export",
                json={"out_path": out},
            )
        except ApiError as exc:
            _handle_api_error(exc)
            return  # pragma: no cover — _handle_api_error raises typer.Exit
    rows = response.get("rows_written", 0)
    console.print(f"wrote {rows} rows to {out}")


@export_app.command("validator-gold")
def validator_gold(
    out: Annotated[
        str,
        typer.Option(
            "--out", "-o",
            help="Path to write the JSONL corpus to.",
        ),
    ],
) -> None:
    """Export labeled validator-gold rows to <out> as JSONL."""
    with _client() as client:
        try:
            response = client._request(
                "POST",
                "/api/v1/dashboard/corpus/validator-gold/export",
                json={"out_path": out},
            )
        except ApiError as exc:
            _handle_api_error(exc)
            return  # pragma: no cover — _handle_api_error raises typer.Exit
    rows = response.get("rows_written", 0)
    console.print(f"wrote {rows} rows to {out}")
