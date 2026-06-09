"""Repo command group — ``treadmill repo add <org/repo>``.

Per ADR-0085+0086 plan Task F, this one-shot command provisions a
coordinator session for a repo:

  1. Derive ``slug`` and ``coordinator_label`` from defaults (or flags).
  2. ``POST /api/v1/team_configs`` to register the
     repo → coordinator-label → worker-labels mapping (Task C lands
     the table + endpoint; this command is the operator-facing wrapper).
  3. Create ``~/.treadmill/teams/<slug>/`` with ``mkdir -p`` semantics.
  4. Write ``~/.treadmill/teams/<slug>/coordinator.env`` with the
     four env vars the coordinator's systemd unit reads (TREADMILL_ROLE,
     TREADMILL_LABEL, TREADMILL_API_URL, TREADMILL_COORDINATOR_PLANS).
  5. ``systemctl --user enable treadmill-channel@<coordinator_label>.service``.
  6. ``systemctl --user start treadmill-channel@<coordinator_label>.service``.
  7. Print a summary.

Idempotent: step 2 is an upsert; step 4 overwrites; steps 5-6 are
no-ops on an already-enabled/running unit.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from treadmill_cli.api_client import ApiClient, ApiError
from treadmill_cli.config import load_config


repo_app = typer.Typer(
    name="repo",
    help="Repo provisioning (ADR-0085+0086: team_configs + coordinator session).",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)


# ── Defaults ────────────────────────────────────────────────────────────


_DEFAULT_WORKER_LABELS = "treadmill-bert,treadmill-donna,treadmill-carla"
_DEFAULT_API_URL = "http://localhost:8000"
_TEAMS_DIR = Path.home() / ".treadmill" / "teams"
_SYSTEMD_UNIT_TEMPLATE = "treadmill-channel@{label}.service"


def _slug_from_repo(repo: str) -> str:
    """Derive the kebab-cased slug from an ``owner/name`` repo string.

    ``slug`` is what we use to namespace ``~/.treadmill/teams/<slug>/``
    + the systemd unit name. Lower-cased + ``/`` replaced with ``-`` so
    the filesystem + systemd template never see ``/``.
    """
    return repo.replace("/", "-").lower()


def _coordinator_label_default(slug: str) -> str:
    """Default coordinator label is ``coordinator-<slug>``."""
    return f"coordinator-{slug}"


def _api_url() -> str:
    """Honor TREADMILL_API_URL env override; fall back to the local default."""
    return os.environ.get("TREADMILL_API_URL", _DEFAULT_API_URL).rstrip("/")


def _coordinator_env_contents(*, label: str, api_url: str) -> str:
    return (
        "TREADMILL_ROLE=coordinator\n"
        f"TREADMILL_LABEL={label}\n"
        f"TREADMILL_API_URL={api_url}\n"
        "TREADMILL_COORDINATOR_PLANS=\n"
    )


def _run_systemctl(args: list[str]) -> tuple[int, str]:
    """Run ``systemctl --user <args>``. Returns ``(returncode, stderr)``.

    Captures stderr so a failure surfaces with context; never raises.
    The caller decides whether the failure is fatal.
    """
    try:
        result = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode, (result.stderr or "").strip()
    except FileNotFoundError:
        return 127, "systemctl not on PATH"
    except Exception as exc:  # pragma: no cover — defensive
        return 1, f"systemctl spawn failed: {exc}"


# ── Command ─────────────────────────────────────────────────────────────


@repo_app.command("add")
def add(
    repo: Annotated[
        str,
        typer.Argument(
            help="Repo in ``owner/name`` form (e.g. ``joeLepper/treadmill``).",
        ),
    ],
    coordinator_label: Annotated[
        str | None,
        typer.Option(
            "--coordinator-label",
            help="Override the default ``coordinator-<slug>``.",
        ),
    ] = None,
    workers: Annotated[
        str,
        typer.Option(
            "--workers",
            help=(
                "Comma-separated worker labels to register with the "
                "team_configs row."
            ),
        ),
    ] = _DEFAULT_WORKER_LABELS,
) -> None:
    """Provision a coordinator session for ``<owner/repo>``.

    Idempotent: ``POST /api/v1/team_configs`` upserts; the env file is
    overwritten; ``systemctl enable/start`` are no-ops if already
    enabled/running.
    """
    if "/" not in repo:
        err_console.print(
            f"[red]repo must be ``owner/name`` form; got {repo!r}[/red]"
        )
        raise typer.Exit(code=1)

    slug = _slug_from_repo(repo)
    label = coordinator_label or _coordinator_label_default(slug)
    worker_labels = [w.strip() for w in workers.split(",") if w.strip()]
    api_url = _api_url()

    # ── Step 1: POST /api/v1/team_configs (upsert) ──────────────────
    with ApiClient(load_config()) as client:
        try:
            client._request(
                "POST",
                "/api/v1/team_configs",
                json={
                    "repo": repo,
                    "coordinator_label": label,
                    "worker_labels": worker_labels,
                },
            )
        except ApiError as exc:
            err_console.print(
                f"[red]team_configs upsert failed: {exc.status_code} "
                f"{exc.detail}[/red]"
            )
            raise typer.Exit(code=2)

    # ── Step 2: ~/.treadmill/teams/<slug>/ + coordinator.env ────────
    team_dir = _TEAMS_DIR / slug
    team_dir.mkdir(parents=True, exist_ok=True)
    env_path = team_dir / "coordinator.env"
    env_path.write_text(
        _coordinator_env_contents(label=label, api_url=api_url)
    )

    # ── Step 3: systemctl --user enable / start ─────────────────────
    unit = _SYSTEMD_UNIT_TEMPLATE.format(label=label)
    systemd_warnings: list[str] = []
    for verb in ("enable", "start"):
        rc, err = _run_systemctl([verb, unit])
        if rc != 0:
            systemd_warnings.append(
                f"systemctl --user {verb} {unit}: rc={rc} stderr={err!r}"
            )

    # ── Step 4: Summary ─────────────────────────────────────────────
    console.print(f"[green]repo[/green]              {repo}")
    console.print(f"[green]coordinator label[/green] {label}")
    console.print(f"[green]worker labels[/green]     {worker_labels}")
    console.print(f"[green]env file[/green]          {env_path}")
    console.print(f"[green]systemd unit[/green]      {unit}")
    if systemd_warnings:
        # Warn loudly but do not raise — `treadmill repo add` should
        # still succeed when the operator is running on a system
        # without systemd-user (CI, container, macOS, etc.). The
        # operator-facing team_configs row + env file are persisted;
        # the systemd hop can be retried by hand.
        err_console.print(
            "[yellow]WARNING: systemd not available or unit failed to "
            "enable/start. team_configs + env file are persisted; "
            "rerun `systemctl --user enable/start` manually if needed.[/yellow]"
        )
        for w in systemd_warnings:
            err_console.print(f"[yellow]  {w}[/yellow]")
