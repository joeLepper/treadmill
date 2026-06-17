"""Team command group — ``treadmill team up <org/repo>``.

Per ADR-0087, a Treadmill team is a per-repo trio: one coordinator + one
evaluator + N workers. All three label families are derived
deterministically from the repo slug at bootstrap time so the operator
does not have to remember (or invent) labels.

This command is the ADR-0087 successor to ``treadmill repo add``
(ADR-0085+0086 Task F PR #275); the older command is preserved as a
deprecated alias in :mod:`treadmill_cli.commands.repo`. After the
deprecation window the alias is removed.

What the command does
=====================

1. Derive ``slug`` from ``<owner>/<name>`` (replace ``/`` with ``-``,
   lowercase). Derive the four label families:

   - ``coordinator-<slug>`` — one PM session for the repo.
   - ``evaluator-<slug>`` — one auditor session for the repo.
   - ``worker-<slug>-1`` … ``worker-<slug>-N`` — implementer sessions.

2. ``POST /api/v1/team_configs`` to upsert the row. The router enforces
   the ADR-0087 scale-down guard server-side (returns 409 when reducing
   worker count would orphan in-flight ``task_executions`` rows);
   ``--force`` is forwarded as ``?force=true`` to skip the guard.

3. Create the per-session directory tree under
   ``~/.treadmill/teams/<slug>/<label>/`` with two files each:

   - ``.session-id`` — empty stub on creation; the coordinator writes
     the actual Claude Code session ID on first subprocess exit and
     reads it on every subsequent ``--resume``.
   - ``<label>.env`` — env vars for the session's systemd unit. The
     env-var shape mirrors the existing coordinator.env pattern.

4. Render the per-session templates (ADR-0087 PR-D/E/H): coordinator +
   evaluator + worker ``CLAUDE.md`` and worker
   ``.claude/settings.json`` via ``tools/team-templates/install.py``'s
   ``install_team()``. Render failures abort (exit 2) — a session that
   boots without its templates is the silent no-op PR-H fixed.

5. ``systemctl --user enable`` + ``start`` for every label's
   ``treadmill-channel@<label>.service`` unit. Systemd failures warn
   but do not abort — the load-bearing artifacts (team_configs row +
   directory tree) survive and the operator can retry the systemd hop
   by hand. Per the Task F design.

Idempotency
-----------

The command is idempotent on a clean re-run:

- ``team_configs`` upsert preserves the row across re-runs.
- Directory tree creation uses ``mkdir(parents=True, exist_ok=True)``.
- ``.session-id`` stub files are not overwritten when they already
  contain a session ID (re-creating the stub would lose the worker's
  accumulated memory).
- ``<label>.env`` files are overwritten on every run (settings update
  per ADR-0087 trumps prior state).
- ``systemctl enable/start`` are platform no-ops on already-enabled or
  already-running units.

Re-running with a different ``--workers N`` resizes the team. Scale-up
(N larger than current) adds the new ``worker-<slug>-K`` labels. Scale-
down (N smaller) is gated by the server-side guard described above.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from treadmill_cli.api_client import ApiClient, ApiError
from treadmill_cli.config import load_config


team_app = typer.Typer(
    name="team",
    help="Team provisioning (ADR-0087: coordinator + evaluator + workers per repo).",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)


# ── Defaults ────────────────────────────────────────────────────────────


_DEFAULT_WORKER_COUNT = 3
_DEFAULT_API_URL = "http://localhost:8000"
_TEAMS_DIR = Path.home() / ".treadmill" / "teams"
_SYSTEMD_UNIT_TEMPLATE = "treadmill-channel@{label}.service"


def _slug_from_repo(repo: str) -> str:
    """Derive the kebab-cased slug from ``<owner>/<name>``."""
    return repo.replace("/", "-").lower()


def _derive_labels(slug: str, n_workers: int) -> tuple[str, str, list[str]]:
    """Return ``(coordinator_label, evaluator_label, worker_labels)`` for ``slug``.

    Per ADR-0087 §Per-repo team shape, label families are derived
    deterministically from the slug. No manual override.
    """
    coordinator = f"coordinator-{slug}"
    evaluator = f"evaluator-{slug}"
    workers = [f"worker-{slug}-{i}" for i in range(1, n_workers + 1)]
    return coordinator, evaluator, workers


def _api_url() -> str:
    """Honor TREADMILL_API_URL env override; fall back to the local default."""
    return os.environ.get("TREADMILL_API_URL", _DEFAULT_API_URL).rstrip("/")


# Per-role model map (task 5d14fbcc): ANTHROPIC_MODEL written into every
# per-label .env so --resume never falls back to the default model.
# INCIDENT 2026-06-12: model-less sessions default to claude-fable-5
# (unavailable on this account) on restart; workers were safe only
# because their workdir .claude/settings.json already pinned sonnet.
# settings.json "model" does NOT override a persisted Fable-5 selection
# on --resume — only the env-var path reaches the subprocess reliably.
_ROLE_MODEL: dict[str, str] = {
    "coordinator": "claude-opus-4-8",
    "evaluator": "claude-opus-4-8",
    "worker": "claude-sonnet-4-6",
}


def _env_contents(*, role: str, label: str, api_url: str) -> str:
    """Compose the per-session env file body.

    ``TREADMILL_ROLE`` carries the session type
    (``coordinator|evaluator|worker``); the launcher reads it to pick
    the correct CLAUDE.md template. ``TREADMILL_LABEL`` is the session
    label that scopes its cc-channels inbox + WS subscription.
    ``ANTHROPIC_MODEL`` pins the Claude model so --resume never falls
    back to the account-default (claude-fable-5 incident 2026-06-12).
    """
    model = _ROLE_MODEL.get(role, "claude-opus-4-8")
    return (
        f"TREADMILL_ROLE={role}\n"
        f"TREADMILL_LABEL={label}\n"
        f"TREADMILL_API_URL={api_url}\n"
        f"ANTHROPIC_MODEL={model}\n"
    )


def _role_for_label(label: str) -> str:
    """Infer role from label prefix.

    Cheap + deterministic given the derivation rules in
    :func:`_derive_labels`. Used to populate ``TREADMILL_ROLE`` in
    each session's env file.
    """
    if label.startswith("coordinator-"):
        return "coordinator"
    if label.startswith("evaluator-"):
        return "evaluator"
    return "worker"


def _ensure_session_tree(
    slug: str, label: str, *, api_url: str, env_extra: str = ""
) -> tuple[Path, Path]:
    """Create ``~/.treadmill/teams/<slug>/<label>/`` if absent.

    Writes:
      - ``.session-id`` — empty stub iff the file does not yet exist.
        Pre-existing ``.session-id`` files (e.g. on re-run after the
        coordinator has captured the real session ID) are LEFT ALONE
        to preserve worker memory.
      - ``<label>.env`` — always rewritten so env-var contract updates
        propagate on every team-up.

    Returns ``(session_id_path, env_path)`` for the caller's summary
    printing.
    """
    session_dir = _TEAMS_DIR / slug / label
    session_dir.mkdir(parents=True, exist_ok=True)
    session_id_path = session_dir / ".session-id"
    if not session_id_path.exists():
        session_id_path.write_text("")
    env_path = session_dir / f"{label}.env"
    body = _env_contents(
        role=_role_for_label(label),
        label=label,
        api_url=api_url,
    )
    if env_extra:
        # Per-team toolchain block (``--env-extra``), appended so it is
        # sourced (``set -a``) at session launch AFTER the standard vars
        # — lets a non-default team type (e.g. osmo: conda + Gemini key +
        # scratch-DB DSN) carry its env without bloating every team.
        body = body + "\n# ── per-team env (--env-extra) ──\n" + env_extra.rstrip("\n") + "\n"
    env_path.write_text(body)
    return session_id_path, env_path


def _repo_root() -> Path:
    """Locate the treadmill repo checkout.

    ``TREADMILL_REPO_DIR`` wins when set (matches the launcher's
    override contract); otherwise derive from this file's location —
    ``<repo>/cli/treadmill_cli/commands/team.py`` → ``parents[3]``.
    Works for the editable install this CLI ships as; a wheel install
    without the repo checkout fails loudly in
    :func:`_install_templates`.
    """
    env = os.environ.get("TREADMILL_REPO_DIR", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3]


def _install_templates(
    repo_slug: str, worker_count: int, pr_base: str = "main"
) -> None:
    """Render the per-session CLAUDE.md + settings.json files.

    Loads ``tools/team-templates/install.py`` from the repo checkout
    by path (it is intentionally not a package — stdlib-only, owned by
    the team-templates component) and calls
    ``install_team(make_team_spec(slug, worker_count))``.

    This is the CLI wiring half of ADR-0087 PR-D/E/H: without it,
    ``team up`` creates the directory tree + systemd units but the
    sessions boot with no rendered CLAUDE.md and no
    ``.claude/settings.json`` (so the worker PostToolUse relay-inject
    hook never registers). See
    docs/learnings/2026-06-10-template-install-layout-vs-launcher-cwd-mismatch.md.
    """
    install_path = _repo_root() / "tools" / "team-templates" / "install.py"
    if not install_path.is_file():
        raise FileNotFoundError(
            f"{install_path} not found — template render requires the "
            "treadmill repo checkout (set TREADMILL_REPO_DIR if it "
            "lives somewhere other than the editable-install location)"
        )
    spec = importlib.util.spec_from_file_location(
        "_treadmill_team_templates_install", install_path
    )
    assert spec is not None and spec.loader is not None  # path checked above
    module = importlib.util.module_from_spec(spec)
    # Register before exec: install.py's @dataclass resolves its string
    # annotations via sys.modules[cls.__module__] — absent registration
    # the dataclass machinery crashes on NoneType.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    module.install_team(
        module.make_team_spec(
            repo_slug, worker_count=worker_count, pr_base=pr_base
        )
    )


def _run_systemctl(args: list[str]) -> tuple[int, str]:
    """Run ``systemctl --user <args>``. Returns ``(returncode, stderr)``.

    Never raises; captures stderr so a failure surfaces with context.
    Matching :mod:`treadmill_cli.commands.repo`'s contract so the
    deprecated alias keeps the same operational behaviour.
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


@team_app.command("up")
def up(
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
                "Number of worker sessions. Deterministically named "
                "``worker-<slug>-1`` through ``worker-<slug>-N``. Default: 3."
            ),
        ),
    ] = _DEFAULT_WORKER_COUNT,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=(
                "Skip the scale-down guard. Honoured by the API: drops "
                "the 409 check that refuses to orphan running "
                "task_executions on to-be-removed worker labels. Use "
                "deliberately."
            ),
        ),
    ] = False,
    slug_override: Annotated[
        str | None,
        typer.Option(
            "--slug",
            help=(
                "Override the label slug (default: derived from the repo). "
                "Labels become ``coordinator-<slug>`` etc. while "
                "``team_configs.repo`` stays the real repo — lets a team's "
                "operating identity differ from the repo owner (e.g. "
                "``--slug joelepper-osmo`` for repo ``osmoai/osmo``)."
            ),
        ),
    ] = None,
    pr_base: Annotated[
        str,
        typer.Option(
            "--pr-base",
            help=(
                "Branch workers branch FROM and PR INTO (default ``main``). "
                "Set to a team trunk (e.g. ``forecast/stage-a``) so workers "
                "never touch the repo's real mainline. Rendered into the "
                "worker template via ``{{PR_BASE}}`` (PR #362)."
            ),
        ),
    ] = "main",
    env_extra: Annotated[
        Path | None,
        typer.Option(
            "--env-extra",
            help=(
                "Path to a file whose contents are appended to every "
                "per-label ``.env`` (sourced at launch). For a non-default "
                "team type that needs its own toolchain env — e.g. osmo: "
                "``source /home/joe/osmo/.env`` + ``STAGEA_DSN`` + "
                "``OSMO_PYTHON``."
            ),
        ),
    ] = None,
) -> None:
    """Provision (or update) a per-repo Treadmill team.

    See module docstring for the full behaviour matrix.
    """
    if "/" not in repo:
        err_console.print(
            f"[red]repo must be ``owner/name`` form; got {repo!r}[/red]"
        )
        raise typer.Exit(code=1)

    env_extra_body = ""
    if env_extra is not None:
        if not env_extra.is_file():
            err_console.print(f"[red]--env-extra file not found: {env_extra}[/red]")
            raise typer.Exit(code=1)
        env_extra_body = env_extra.read_text()

    slug = slug_override or _slug_from_repo(repo)
    coordinator_label, evaluator_label, worker_labels = _derive_labels(
        slug, workers
    )
    api_url = _api_url()
    all_labels = [coordinator_label, evaluator_label, *worker_labels]

    # ── Step 1: POST /api/v1/team_configs (upsert, with scale-down guard) ─
    upsert_path = "/api/v1/team_configs"
    if force:
        upsert_path = f"{upsert_path}?force=true"
    with ApiClient(load_config()) as client:
        try:
            client._request(
                "POST",
                upsert_path,
                json={
                    "repo": repo,
                    "coordinator_label": coordinator_label,
                    "evaluator_label": evaluator_label,
                    "worker_labels": worker_labels,
                },
            )
        except ApiError as exc:
            if exc.status_code == 409:
                err_console.print(
                    f"[red]scale-down refused (HTTP 409): {exc.detail}[/red]"
                )
                err_console.print(
                    "[yellow]Re-run with --force to override after "
                    "confirming the in-flight work is recoverable.[/yellow]"
                )
                raise typer.Exit(code=2)
            err_console.print(
                f"[red]team_configs upsert failed: {exc.status_code} "
                f"{exc.detail}[/red]"
            )
            raise typer.Exit(code=2)

    # ── Step 2: per-session directory tree + .session-id stubs ─────
    session_id_paths: list[Path] = []
    env_paths: list[Path] = []
    for label in all_labels:
        sid, env = _ensure_session_tree(
            slug, label, api_url=api_url, env_extra=env_extra_body
        )
        session_id_paths.append(sid)
        env_paths.append(env)

    # ── Step 3: render per-session templates (ADR-0087 PR-D/E/H) ───
    # Must happen BEFORE the systemd start so sessions boot with their
    # CLAUDE.md + .claude/settings.json already on disk. Hard failure:
    # a team whose sessions boot without templates is the exact silent
    # no-op PR-H fixed — fail loudly and let the operator re-run.
    try:
        _install_templates(slug, workers, pr_base=pr_base)
    except Exception as exc:
        err_console.print(f"[red]template render failed: {exc}[/red]")
        err_console.print(
            "[yellow]team_configs row + directory tree are persisted; "
            "fix the cause and re-run `treadmill team up` (idempotent)."
            "[/yellow]"
        )
        raise typer.Exit(code=2)

    # ── Step 4: systemctl --user enable / start per session ────────
    systemd_warnings: list[str] = []
    for label in all_labels:
        unit = _SYSTEMD_UNIT_TEMPLATE.format(label=label)
        for verb in ("enable", "start"):
            rc, err = _run_systemctl([verb, unit])
            if rc != 0:
                systemd_warnings.append(
                    f"systemctl --user {verb} {unit}: rc={rc} stderr={err!r}"
                )

    # ── Step 5: Summary ─────────────────────────────────────────────
    console.print(f"[green]repo[/green]              {repo}")
    console.print(f"[green]slug[/green]              {slug}")
    console.print(f"[green]coordinator label[/green] {coordinator_label}")
    console.print(f"[green]evaluator label[/green]   {evaluator_label}")
    console.print(f"[green]worker labels[/green]     {worker_labels}")
    console.print(
        f"[green]team dir[/green]          {_TEAMS_DIR / slug}"
    )
    if systemd_warnings:
        err_console.print(
            "[yellow]WARNING: systemd not available or unit failed to "
            "enable/start. team_configs row + directory tree are "
            "persisted; rerun `systemctl --user enable/start` "
            "manually if needed.[/yellow]"
        )
        for w in systemd_warnings:
            err_console.print(f"[yellow]  {w}[/yellow]")
