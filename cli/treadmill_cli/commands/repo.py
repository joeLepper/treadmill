"""Repo command group — DEPRECATED alias for ``treadmill team up``.

This module preserves the ADR-0085+0086 Task F surface
(``treadmill repo add <org/repo>``) as a compatibility shim during the
ADR-0087 deprecation window. The canonical command is now
``treadmill team up`` (see :mod:`treadmill_cli.commands.team`).

Behaviour
=========

``treadmill repo add <org/repo>`` forwards verbatim to
``treadmill team up <org/repo>`` and emits a ``DeprecationWarning`` to
stderr. The implementation imports + delegates; there is no code
duplication between the two.

Removal
=======

This alias is removed in the next CLI minor version. The ADR-0087
implementation plan (Phase 5) drops it along with the workflow
versioning cleanup.
"""

from __future__ import annotations

import warnings
from typing import Annotated

import typer
from rich.console import Console

from treadmill_cli.commands.team import up as _team_up


repo_app = typer.Typer(
    name="repo",
    help=(
        "DEPRECATED: repo provisioning. Use ``treadmill team up`` instead. "
        "This alias is removed in the next minor version."
    ),
    no_args_is_help=True,
)

err_console = Console(stderr=True)


@repo_app.command("add")
def add(
    repo: Annotated[
        str,
        typer.Argument(
            help="Repo in ``owner/name`` form (e.g. ``joeLepper/treadmill``).",
        ),
    ],
    workers: Annotated[
        int,
        typer.Option(
            "--workers",
            min=1,
            help=(
                "Number of worker sessions (default 3). Per ADR-0087, "
                "worker labels are derived deterministically as "
                "``worker-<slug>-1`` … ``worker-<slug>-N``."
            ),
        ),
    ] = 3,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Forwarded to the scale-down guard. See `treadmill team up --help`.",
        ),
    ] = False,
) -> None:
    """DEPRECATED alias for ``treadmill team up``. Forwards verbatim."""
    err_console.print(
        "[yellow]WARNING: `treadmill repo add` is deprecated; use "
        "`treadmill team up` instead. This alias is removed in the "
        "next minor version.[/yellow]"
    )
    warnings.warn(
        "treadmill repo add is deprecated; use `treadmill team up`",
        DeprecationWarning,
        stacklevel=2,
    )
    _team_up(repo=repo, workers=workers, force=force)
