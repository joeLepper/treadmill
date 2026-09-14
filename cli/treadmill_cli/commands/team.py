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

import fcntl
import importlib.util
import os
import subprocess
import sys
from datetime import datetime, timezone
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
# Per-repo mode choices (ADR-0109 / ADR-0110). Mirror the API's Literal enums;
# the CLI validates client-side for a clean error before the round-trip.
_LIFECYCLE_CHOICES = frozenset({"ephemeral", "persistent", "manual"})
_MERGE_TARGET_CHOICES = frozenset({"feature-branch", "main"})


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
        # — lets a non-default team type (e.g. zephyr: conda + Gemini key +
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
                "``--slug joelepper-zephyr`` for repo ``ZEPHYR/zephyr``)."
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
                "team type that needs its own toolchain env — e.g. zephyr: "
                "``source /home/joe/zephyr/.env`` + ``STAGEA_DSN`` + "
                "``ZEPHYR_PYTHON``."
            ),
        ),
    ] = None,
    lifecycle: Annotated[
        str | None,
        typer.Option(
            "--lifecycle",
            help=(
                "Team lifecycle mode (ADR-0109): ``ephemeral`` (stand up per "
                "plan, tear down when done), ``persistent`` (never auto-down), "
                "or ``manual`` (up/down by command only). Omit to keep the "
                "repo's current mode (new repos default ``ephemeral``)."
            ),
        ),
    ] = None,
    merge_target: Annotated[
        str | None,
        typer.Option(
            "--merge-target",
            help=(
                "Where the team integrates (ADR-0110): ``feature-branch`` (the "
                "team merges into a per-plan ``joes-agents/<slug>`` branch and "
                "the human owns the branch->main PR) or ``main`` (agent-merge "
                "to main; rare). Omit to keep the repo's current target (new "
                "repos default ``feature-branch``)."
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

    if lifecycle is not None and lifecycle not in _LIFECYCLE_CHOICES:
        err_console.print(
            f"[red]--lifecycle must be one of "
            f"{sorted(_LIFECYCLE_CHOICES)}; got {lifecycle!r}[/red]"
        )
        raise typer.Exit(code=1)
    if merge_target is not None and merge_target not in _MERGE_TARGET_CHOICES:
        err_console.print(
            f"[red]--merge-target must be one of "
            f"{sorted(_MERGE_TARGET_CHOICES)}; got {merge_target!r}[/red]"
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
    upsert_body: dict[str, object] = {
        "repo": repo,
        "coordinator_label": coordinator_label,
        "evaluator_label": evaluator_label,
        "worker_labels": worker_labels,
    }
    # Send a mode only when the operator set it; omitting lets the API keep the
    # repo's current value (or apply the server-default on first insert).
    if lifecycle is not None:
        upsert_body["lifecycle"] = lifecycle
    if merge_target is not None:
        upsert_body["merge_target"] = merge_target
    with ApiClient(load_config()) as client:
        try:
            client._request("POST", upsert_path, json=upsert_body)
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


def _all_team_labels(cfg: dict) -> list[str]:
    """Coordinator + evaluator (if any) + workers, in teardown order."""
    labels = [cfg["coordinator_label"]]
    if cfg.get("evaluator_label"):
        labels.append(cfg["evaluator_label"])
    labels.extend(cfg.get("worker_labels", []))
    return labels


def _teardown_team_units(cfg: dict) -> list[str]:
    """Stop + disable every team unit. Returns systemd warnings (empty on success).
    Shared by ``team down`` and ``team sweep`` so both tear down identically."""
    warnings: list[str] = []
    for label in _all_team_labels(cfg):
        unit = _SYSTEMD_UNIT_TEMPLATE.format(label=label)
        rc, err = _run_systemctl(["disable", "--now", unit])
        if rc != 0:
            warnings.append(
                f"systemctl --user disable --now {unit}: rc={rc} stderr={err!r}"
            )
    return warnings


@team_app.command("down")
def down(
    repo: Annotated[
        str,
        typer.Argument(help="Repo in ``owner/name`` form (e.g. ``joeLepper/treadmill``)."),
    ],
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Tear down even if the drain-guard reports in-flight work "
            "(explicitly abandons it).",
        ),
    ] = False,
) -> None:
    """Tear down a team (ADR-0109): drain-guarded stop + disable of every team unit.

    Refuses (exit 2) when the ADR-0109 drain-guard reports TEAM-ACTIVE work (a
    non-terminal, non-escalated task) or a merged task with an unsettled deploy.
    `escalated` tasks are PARKED-ON-HUMAN and do NOT block (they are tracked for
    re-standup). The rendered dir + team_config row are KEPT (revivable). `--force`
    overrides the guard and abandons the in-flight work.
    """
    if "/" not in repo:
        err_console.print("[red]repo must be in owner/name form[/red]")
        raise typer.Exit(code=1)
    slug = _slug_from_repo(repo)

    with ApiClient(load_config()) as client:
        # Authoritative labels from the team_config row.
        try:
            cfg = client._request("GET", f"/api/v1/team_configs/{repo}")
        except ApiError as exc:
            if exc.status_code == 404:
                err_console.print(
                    f"[yellow]no team_config for {repo}; nothing to tear down[/yellow]"
                )
                raise typer.Exit(code=0)
            err_console.print(
                f"[red]team_config fetch failed: {exc.status_code} {exc.detail}[/red]"
            )
            raise typer.Exit(code=2)
        # Drain-guard (server-side, joins task state + escalations + post-merge deploy).
        try:
            drain = client._request("GET", f"/api/v1/team_configs/{repo}/drain")
        except ApiError as exc:
            err_console.print(
                f"[red]drain-guard check failed: {exc.status_code} {exc.detail}[/red]"
            )
            raise typer.Exit(code=2)

    if not drain.get("clean", False) and not force:
        err_console.print(
            f"[red]team down REFUSED: drain-guard reports in-flight work on {repo}.[/red]"
        )
        for item in drain.get("blocking", []):
            err_console.print(
                f"[yellow]  {item['task_id']} — {item.get('derived_status')} "
                f"({item['reason']})[/yellow]"
            )
        if drain.get("parked"):
            err_console.print(
                f"[dim]  parked-on-human (not blocking): "
                f"{len(drain['parked'])} escalated task(s)[/dim]"
            )
        err_console.print(
            "[yellow]Wait for the work to reach a terminal state, or --force to "
            "abandon it.[/yellow]"
        )
        raise typer.Exit(code=2)

    # Stop + disable every team unit; keep the rendered dir + config (revivable).
    all_labels = _all_team_labels(cfg)
    systemd_warnings = _teardown_team_units(cfg)

    console.print(f"[green]team down[/green]         {repo}")
    console.print(f"[green]stopped labels[/green]    {all_labels}")
    console.print(f"[green]team dir kept[/green]     {_TEAMS_DIR / slug} (revivable)")
    if force and not drain.get("clean", False):
        err_console.print(
            "[yellow]--force: tore down with in-flight work the drain-guard "
            "reported abandoned.[/yellow]"
        )
    if systemd_warnings:
        err_console.print(
            "[yellow]WARNING: systemd not available or unit failed to "
            "disable/stop; the units may still be running — retry by hand.[/yellow]"
        )
        for w in systemd_warnings:
            err_console.print(f"[yellow]  {w}[/yellow]")


_DEFAULT_IDLE_HOURS = 6.0


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp from the API into an aware datetime, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Treat a naive timestamp as UTC (the API serializes tz-aware).
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _sweep_decision(
    cfg: dict, drain: dict, now: datetime, idle_hours: float
) -> str:
    """Pure teardown decision shared by `team sweep` and `team reconcile` so both
    honor the SAME drain-guard (Ernie 4b #4 — no shortcut that bypasses the handoff
    gate). Returns one of: sweep | skip-mode | skip-active | skip-grace | skip-new.
    A team is swept only when ephemeral AND drain-clean (the drain-guard's verdict,
    which keeps parked-on-human green-with-tracking) AND idle beyond the grace."""
    if cfg.get("lifecycle") != "ephemeral":
        return "skip-mode"
    if not drain.get("clean", False):
        return "skip-active"
    last = _parse_ts(drain.get("last_activity_at"))
    if last is None:
        return "skip-new"
    if (now - last).total_seconds() / 3600.0 < idle_hours:
        return "skip-grace"
    return "sweep"


def _coordinator_unit_active(cfg: dict) -> bool:
    """True iff the coordinator's systemd --user unit is actually running. Team
    LIVENESS is the unit state, NOT the lease/config row (Ernie 4b #2): a team whose
    config row exists but whose unit is down (a crash, or a `team down`) is NOT live
    and must be revived if it still has work."""
    unit = _SYSTEMD_UNIT_TEMPLATE.format(label=cfg["coordinator_label"])
    # `systemctl is-active` exits 0 iff the unit is active (3 = inactive/failed).
    # _run_systemctl captures stderr, not stdout, so the exit code is the signal.
    rc, _ = _run_systemctl(["is-active", unit])
    return rc == 0


def _revive_team_units(cfg: dict) -> list[str]:
    """Enable + start every team unit (the standup side-effect for a REVIVE). The
    rendered team dir + templates persist across `team down` (kept, revivable), so a
    revive is a unit restart, not a re-render. Returns systemd warnings."""
    warnings: list[str] = []
    for label in _all_team_labels(cfg):
        unit = _SYSTEMD_UNIT_TEMPLATE.format(label=label)
        for verb in ("enable", "start"):
            rc, err = _run_systemctl([verb, unit])
            if rc != 0:
                warnings.append(
                    f"systemctl --user {verb} {unit}: rc={rc} stderr={err!r}"
                )
    return warnings


@team_app.command("sweep")
def sweep(
    idle_hours: Annotated[
        float,
        typer.Option(
            "--idle-hours",
            min=0.0,
            help=(
                "Idle-grace threshold. A team is swept only when its last activity "
                "is older than this many hours. Prevents thrash right after a plan "
                f"ends. Default: {_DEFAULT_IDLE_HOURS}."
            ),
        ),
    ] = _DEFAULT_IDLE_HOURS,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Report which teams WOULD be swept without tearing any down.",
        ),
    ] = False,
) -> None:
    """Idle-sweep (ADR-0109): tear down every EPHEMERAL team that is drain-clean
    and idle beyond the grace threshold.

    Only ``ephemeral`` teams are swept — ``persistent`` and ``manual`` teams are
    never auto-torn-down. A team is swept only when BOTH hold: the drain-guard
    reports it clean (no team-active work, no unsettled post-merge deploy), AND its
    last activity is older than ``--idle-hours``. A team with in-flight work, or one
    whose last activity is within the grace window, or one with no tasks yet (no age
    to measure), is left alone. Safe to run on a timer.
    """
    now = datetime.now(timezone.utc)
    swept: list[str] = []
    skipped_mode: list[str] = []
    skipped_active: list[str] = []
    skipped_grace: list[str] = []
    skipped_new: list[str] = []
    errors: list[str] = []

    with ApiClient(load_config()) as client:
        try:
            configs = client._request("GET", "/api/v1/team_configs")
        except ApiError as exc:
            err_console.print(
                f"[red]team_configs list failed: {exc.status_code} {exc.detail}[/red]"
            )
            raise typer.Exit(code=2)

        _bucket = {
            "skip-mode": skipped_mode,
            "skip-active": skipped_active,
            "skip-grace": skipped_grace,
            "skip-new": skipped_new,
        }
        for cfg in configs:
            repo = cfg["repo"]
            if cfg.get("lifecycle") != "ephemeral":
                skipped_mode.append(repo)
                continue
            try:
                drain = client._request(
                    "GET", f"/api/v1/team_configs/{repo}/drain"
                )
            except ApiError as exc:
                errors.append(f"{repo}: drain {exc.status_code} {exc.detail}")
                continue
            decision = _sweep_decision(cfg, drain, now, idle_hours)
            if decision != "sweep":
                _bucket[decision].append(repo)
                continue
            # Qualifies: ephemeral + clean + idle beyond grace.
            if dry_run:
                swept.append(repo)
                continue
            warnings = _teardown_team_units(cfg)
            swept.append(repo)
            if warnings:
                for w in warnings:
                    err_console.print(f"[yellow]  {repo}: {w}[/yellow]")

    verb = "would sweep" if dry_run else "swept"
    console.print(f"[green]{verb}[/green]            {swept}")
    console.print(f"[dim]skipped non-ephemeral {skipped_mode}[/dim]")
    console.print(f"[dim]skipped in-flight     {skipped_active}[/dim]")
    console.print(f"[dim]skipped within grace  {skipped_grace}[/dim]")
    console.print(f"[dim]skipped no-activity   {skipped_new}[/dim]")
    if errors:
        err_console.print("[yellow]drain errors (left standing):[/yellow]")
        for e in errors:
            err_console.print(f"[yellow]  {e}[/yellow]")


_RECONCILE_LOCK = _TEAMS_DIR.parent / "team-reconcile.lock"


@team_app.command("reconcile")
def reconcile(
    idle_hours: Annotated[
        float,
        typer.Option(
            "--idle-hours",
            min=0.0,
            help=(
                "Idle-grace before an ephemeral team is torn down (shared with "
                f"`team sweep`). Default: {_DEFAULT_IDLE_HOURS}."
            ),
        ),
    ] = _DEFAULT_IDLE_HOURS,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Report actions without touching systemd."),
    ] = False,
) -> None:
    """Reconcile team liveness against work (ADR-0109 watcher, step 4b).

    One periodic pass, single-flighted by a host lock. For every non-`manual` team:
    STAND UP a team that has work (drain NOT clean) but whose unit is not live —
    covering both a resolved escalation (drain becomes not-clean again) and a
    crashed/half-done standup (liveness is the UNIT, not the lease row); and TEAR
    DOWN an ephemeral team that is drain-clean and idle beyond the grace (the same
    drain-guard `team sweep` uses, so parked-on-human stays green-with-tracking).
    A `manual` team is never auto-managed. Safe to run on a systemd timer.
    """
    # Host single-flight: two overlapping passes would double systemctl work and
    # interleave teardown/standup. A non-blocking flock makes a concurrent tick a
    # no-op rather than a race.
    _RECONCILE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = open(_RECONCILE_LOCK, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        console.print("[dim]reconcile already running on this host; skipping[/dim]")
        lock_fh.close()
        raise typer.Exit(code=0)

    now = datetime.now(timezone.utc)
    revived: list[str] = []
    torn_down: list[str] = []
    left_working: list[str] = []
    left_running: list[str] = []
    left_down: list[str] = []
    skipped_manual: list[str] = []
    errors: list[str] = []
    try:
        with ApiClient(load_config()) as client:
            try:
                configs = client._request("GET", "/api/v1/team_configs")
            except ApiError as exc:
                err_console.print(
                    f"[red]team_configs list failed: {exc.status_code} "
                    f"{exc.detail}[/red]"
                )
                raise typer.Exit(code=2)

            for cfg in configs:
                repo = cfg["repo"]
                if cfg.get("lifecycle") == "manual":
                    skipped_manual.append(repo)
                    continue
                try:
                    drain = client._request(
                        "GET", f"/api/v1/team_configs/{repo}/drain"
                    )
                except ApiError as exc:
                    # Fail-closed: an unconfirmable drain neither revives nor tears
                    # down — leave the team exactly as it is.
                    errors.append(f"{repo}: drain {exc.status_code} {exc.detail}")
                    continue

                live = _coordinator_unit_active(cfg)
                has_work = not drain.get("clean", False)

                if has_work:
                    if live:
                        left_working.append(repo)
                        continue
                    # Not live but has work → STAND UP (revive). Covers a resolved
                    # escalation (drain not-clean again) and a crashed standup
                    # (row exists, unit down).
                    if dry_run:
                        revived.append(repo)
                        continue
                    warnings = _revive_team_units(cfg)
                    revived.append(repo)
                    for w in warnings:
                        err_console.print(f"[yellow]  {repo}: {w}[/yellow]")
                    continue

                # Drain clean → maybe TEAR DOWN (only ephemeral + idle + live).
                decision = _sweep_decision(cfg, drain, now, idle_hours)
                if decision == "sweep" and live:
                    if dry_run:
                        torn_down.append(repo)
                        continue
                    warnings = _teardown_team_units(cfg)
                    torn_down.append(repo)
                    for w in warnings:
                        err_console.print(f"[yellow]  {repo}: {w}[/yellow]")
                elif live:
                    # Clean + up but not swept (persistent, within grace, or no
                    # activity yet) — the team is RUNNING, not "left down".
                    left_running.append(repo)
                else:
                    left_down.append(repo)
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()

    verb_up = "would revive" if dry_run else "revived"
    verb_down = "would tear down" if dry_run else "tore down"
    console.print(f"[green]{verb_up}[/green]        {revived}")
    console.print(f"[green]{verb_down}[/green]     {torn_down}")
    console.print(f"[dim]left working          {left_working}[/dim]")
    console.print(f"[dim]left running (clean)  {left_running}[/dim]")
    console.print(f"[dim]left down (no work)   {left_down}[/dim]")
    console.print(f"[dim]skipped manual        {skipped_manual}[/dim]")
    if errors:
        err_console.print("[yellow]drain errors (left as-is):[/yellow]")
        for e in errors:
            err_console.print(f"[yellow]  {e}[/yellow]")
