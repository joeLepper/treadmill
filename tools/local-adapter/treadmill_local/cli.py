"""Treadmill local adapter CLI.

Commands:
  up      — synth CDK and start the local substrate (moto + Docker containers)
  down    — tear down the local substrate cleanly
  status  — show what's running and what's not
  logs    — tail logs for a specific container or all containers
  init    — populate ``~/.treadmill/<deployment_id>.yaml`` from a deployed
            ``TreadmillCloudLite`` stack's CloudFormation outputs
  docs    — pull/push/list/get docs over the doc REST API (ADR-0054)
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import boto3
import httpx
import typer
from rich.console import Console

from treadmill_local.deployment_config import (
    build_deployment_config,
    load_deployment_yaml,
    read_stack_outputs,
    write_deployment_yaml,
)
from treadmill_local.docs_sync import get_doc, list_docs, pull, push
from treadmill_local.onboard import build_profile, infer_repo, onboard_payload
from treadmill_local.repos import init_bare_repo
from treadmill_local.runtime import BARE_REPOS_DIR, LocalRuntime, find_repo_root

app = typer.Typer(
    name="treadmill-local",
    help="Treadmill local adapter — run the same CDK stack on moto + Docker.",
    no_args_is_help=True,
    add_completion=False,
)


# Captured at callback time so commands that need the operator's original
# working directory (e.g. ``repo onboard``, which inspects the cwd's git
# remote and checkout) can recover it after the chdir-to-repo-root anchor
# fires. ``None`` until the callback has run.
_INVOCATION_CWD: Path | None = None


@app.callback()
def _chdir_to_repo_root() -> None:
    """Anchor cwd to the repo root before any subcommand runs.

    The runtime's ``STATE_DIR`` (and all derived PID / log / state-file
    paths) is a relative ``Path(".treadmill-local")`` that resolves against
    cwd. Without this callback, where the operator invoked
    ``treadmill-local`` from changes where state ends up — e.g.,
    ``uv run --project tools/local-adapter ...`` would write to
    ``tools/local-adapter/.treadmill-local/`` while a bare invocation from
    the repo root wrote to ``./.treadmill-local/``. The latter is the
    canonical home, so we chdir there once at command boundary.

    Spawned subprocesses (autoscaler, scheduler, deploy-watcher) inherit
    this cwd via ``subprocess.Popen(..., cwd=str(Path.cwd()))`` in the
    runtime, so their relative state paths land in the same dir.
    """
    global _INVOCATION_CWD
    _INVOCATION_CWD = Path.cwd()
    os.chdir(find_repo_root())
repo_app = typer.Typer(
    name="repo",
    help="Manage local bare repos for the agent worker's REPO_MODE=local.",
    no_args_is_help=True,
)
app.add_typer(repo_app)
console = Console()


def _load_deployment_or_exit(deployment_id: str | None) -> dict | None:
    """Load the deployment YAML for *deployment_id*, exiting cleanly on error.

    Returns ``None`` when *deployment_id* is falsy (fully-local mode).
    Exits with code 2 (and a friendly message) when the YAML is missing
    or malformed — operator-clarity beats a Python traceback.
    """
    if not deployment_id:
        return None
    try:
        return load_deployment_yaml(deployment_id)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc


def _runtime(
    infra_dir: Path,
    *,
    deployment_config: dict | None = None,
    build_images: bool = True,
    start_autoscaler: bool = True,
    start_scheduler: bool = True,
    start_deploy_watcher: bool = True,
    start_observability: bool = True,
) -> LocalRuntime:
    # Fully-local mode requires cdk.json (it shells out to ``cdk synth``).
    # Dev-local skips synth entirely, so the cdk.json check is also
    # skipped — the operator may run ``up --deployment`` from a checkout
    # that's never had CDK initialized.
    if deployment_config is None and not (infra_dir / "cdk.json").exists():
        console.print(f"[red]No cdk.json found in {infra_dir}[/red]")
        raise typer.Exit(code=2)
    return LocalRuntime(
        infra_dir=infra_dir,
        deployment_config=deployment_config,
        build_images=build_images,
        start_autoscaler=start_autoscaler,
        start_scheduler=start_scheduler,
        start_deploy_watcher=start_deploy_watcher,
        start_observability=start_observability,
    )


@app.command()
def up(
    infra: Path = typer.Option(
        Path("infra"),
        "--infra",
        help="Path to the CDK app directory (containing cdk.json).",
    ),
    deployment: str | None = typer.Option(
        None,
        "--deployment", "-d",
        help="Deployment slug (e.g. 'personal') — switches to dev-local mode. "
             "Reads ~/.treadmill/<deployment>.yaml and starts Postgres + "
             "Redis + API against real AWS endpoints (no moto).",
    ),
    no_build: bool = typer.Option(
        False,
        "--no-build",
        help="Skip rebuilding treadmill-api:dev / treadmill-agent:dev "
             "before starting containers. Default is to always rebuild "
             "(Docker's layer cache makes this near-free when nothing "
             "changed) to prevent silently running stale code. Use this "
             "flag only when debugging with a known-good image.",
    ),
    no_autoscaler: bool = typer.Option(
        False,
        "--no-autoscaler",
        help="Skip starting the autoscaler subprocess. Default is to "
             "always start it (per ADR-0018 / ADR-0019): the autoscaler "
             "polls the work queue and spawns worker containers on "
             "demand. Use this flag when debugging a specific worker "
             "failure in isolation with manual ``run-worker`` control.",
    ),
    no_scheduler: bool = typer.Option(
        False,
        "--no-scheduler",
        help="Skip starting the scheduler subprocess (dev-local only). "
             "Default is to always start it: the scheduler polls Postgres "
             "for active cron schedules and fires ticks on the event bus. "
             "Use this flag when running schedule-free workflows or when "
             "debugging without the cron-dispatch loop.",
    ),
    no_deploy_watcher: bool = typer.Option(
        False,
        "--no-deploy-watcher",
        help="Skip starting the deploy-watcher subprocess (dev-local only). "
             "Default is to always start it: the deploy-watcher polls the "
             "deploy-events SQS queue and reconciles local containers when "
             "PRs are merged. Use this flag when debugging without automated "
             "deploy reconciliation.",
    ),
    no_observability: bool = typer.Option(
        False,
        "--no-observability",
        help="Skip starting the observability compose stack (dev-local only). "
             "Default is to always start it (per ADR-0043): the stack runs "
             "Loki + Prometheus + Tempo + Grafana + OTel collector via "
             "docker compose. Use this flag when debugging without "
             "observability or when memory pressure on the operator's "
             "laptop matters more than dashboards.",
    ),
) -> None:
    """Synth CDK + provision moto + start support containers (fully-local),
    or start Postgres + Redis + API against real AWS (dev-local, with
    ``--deployment``)."""
    cfg = _load_deployment_or_exit(deployment)
    rt = _runtime(
        infra,
        deployment_config=cfg,
        build_images=not no_build,
        start_autoscaler=not no_autoscaler,
        start_scheduler=not no_scheduler,
        start_deploy_watcher=not no_deploy_watcher,
        start_observability=not no_observability,
    )
    rt.up()


@app.command()
def redeploy(
    deployment: str = typer.Option(
        ...,
        "--deployment", "-d",
        help="Deployment slug (required). Reads ~/.treadmill/<deployment>.yaml "
             "to find the AWS profile + region + stack name.",
    ),
    infra: Path = typer.Option(
        Path("infra"),
        "--infra",
        help="Path to the CDK app directory (containing cdk.json).",
    ),
    no_cdk: bool = typer.Option(
        False,
        "--no-cdk",
        help="Skip the ``cdk deploy`` step. Useful when only worker/API "
             "code changed (the auto-rebuild in ``up`` handles those) and "
             "no infra/CDK files were modified. Saves ~30-90s of synth + "
             "no-op deploy time.",
    ),
    no_build: bool = typer.Option(
        False,
        "--no-build",
        help="Skip rebuilding treadmill-api:dev / treadmill-agent:dev "
             "during the up phase. Mirrors ``up --no-build``.",
    ),
    no_autoscaler: bool = typer.Option(
        False,
        "--no-autoscaler",
        help="Skip starting the autoscaler subprocess. Mirrors "
             "``up --no-autoscaler``.",
    ),
) -> None:
    """End-to-end redeploy: cdk deploy → down → up.

    The intended flow after merging a PR that touched infra,
    services/api, or workers/agent code:

      treadmill-local redeploy --deployment personal

    The ``cdk deploy`` step is idempotent — passing it through every
    redeploy is cheap if nothing changed (a few seconds of synth +
    a no-op CloudFormation check). The Postgres alembic upgrade runs
    automatically inside ``up`` (per the API's CLI entrypoint
    fix); no separate step needed.

    Fully-local mode (no ``--deployment``) is not a valid use case
    for this command — the AWS-side step is the value-add. Use
    ``up`` directly for fully-local.

    The flow fails fast on any step error; subsequent steps are
    skipped so the operator can investigate without a half-cycled
    stack.
    """
    cfg = _load_deployment_or_exit(deployment)
    if cfg is None:
        console.print(
            "[red]redeploy requires --deployment <slug> "
            "(fully-local has no AWS to redeploy)[/red]"
        )
        raise typer.Exit(code=2)

    rt = _runtime(
        infra,
        deployment_config=cfg,
        build_images=not no_build,
        start_autoscaler=not no_autoscaler,
    )
    rt.redeploy(skip_cdk=no_cdk)


@app.command()
def down(
    infra: Path = typer.Option(
        Path("infra"),
        "--infra",
        help="Path to the CDK app directory.",
    ),
    deployment: str | None = typer.Option(
        None,
        "--deployment", "-d",
        help="Deployment slug. Optional for ``down`` — teardown is the "
             "same for both modes (stop every Treadmill-managed container).",
    ),
) -> None:
    """Tear down the local substrate cleanly."""
    cfg = _load_deployment_or_exit(deployment)
    rt = _runtime(infra, deployment_config=cfg)
    rt.down()


@app.command()
def status(
    infra: Path = typer.Option(
        Path("infra"),
        "--infra",
        help="Path to the CDK app directory.",
    ),
    deployment: str | None = typer.Option(
        None,
        "--deployment", "-d",
        help="Deployment slug. Optional for ``status``.",
    ),
) -> None:
    """Show what's running."""
    cfg = _load_deployment_or_exit(deployment)
    rt = _runtime(infra, deployment_config=cfg)
    rt.status()


@app.command()
def logs(
    container: str = typer.Argument(..., help="Container name or 'all'."),
    follow: bool = typer.Option(False, "-f", "--follow", help="Stream logs."),
) -> None:
    """Tail logs for a container."""
    LocalRuntime.logs(container, follow=follow)


@app.command(name="run-worker")
def run_worker(
    family: str = typer.Argument(..., help="ECS task definition family name."),
    infra: Path = typer.Option(
        Path("infra"),
        "--infra",
        help="Path to the CDK app directory.",
    ),
    deployment: str | None = typer.Option(
        None,
        "--deployment", "-d",
        help="Deployment slug. When set, the worker is configured for the "
             "dev-local deployment (real AWS, github repo mode).",
    ),
    no_build: bool = typer.Option(
        False,
        "--no-build",
        help="Skip rebuilding treadmill-api:dev / treadmill-agent:dev "
             "before launching the worker. Default is to always rebuild — "
             "Docker's layer cache makes this near-free when nothing "
             "changed and prevents silently running stale worker code "
             "when ``run-worker`` is invoked against an already-up stack.",
    ),
) -> None:
    """Start one worker container for the given task family.

    The worker exits after one message (EXIT_AFTER_STEP=true). On Day 3 the
    autoscaler will run these on demand; for now this command starts them
    explicitly.
    """
    cfg = _load_deployment_or_exit(deployment)
    rt = _runtime(infra, deployment_config=cfg, build_images=not no_build)
    container = rt.start_worker_once(family)
    console.print(f"  container id: [cyan]{container.short_id}[/cyan]")
    console.print(f"  follow logs:  [dim]treadmill-local logs {container.name} -f[/dim]")


@repo_app.command(name="init")
def repo_init(
    repo: str = typer.Argument(..., help='Slash-separated owner/name (e.g. "treadmill/treadmill").'),
) -> None:
    """Provision a local bare repo for ``REPO_MODE=local``.

    Creates ``.treadmill-local/repos/<owner>__<name>.git`` with one
    initial commit on ``main`` so workers can clone, branch, commit,
    and push without a remote service.
    """
    bare = init_bare_repo(BARE_REPOS_DIR, repo)
    console.print(f"  bare repo: [cyan]{bare}[/cyan]")
    console.print('  workers in REPO_MODE=local will see this as file:///var/treadmill/repos/...')


def _stack_name_for(deployment_id: str) -> str:
    """Compute the default CFN stack name from *deployment_id*.

    Mirrors ``infra/treadmill_infra/stacks/cloud_lite.py`` so the operator
    doesn't have to remember the PascalCase derivation. ``personal`` →
    ``TreadmillPersonalCloudLite``.
    """
    return f"Treadmill{deployment_id.title().replace('_', '')}CloudLite"


def _obs_stack_name_for(deployment_id: str) -> str:
    """Compute the observability stack CFN name from *deployment_id*.

    Mirrors ``infra/treadmill_infra/stacks/observability.py``.
    ``personal`` → ``TreadmillPersonalObservability``.
    """
    return f"Treadmill{deployment_id.title().replace('_', '')}Observability"


@app.command(name="init")
def init(
    deployment_id: str = typer.Argument(
        ...,
        help='Deployment slug (e.g. "personal"). Must match the CDK '
             "deployment_id used at deploy time.",
    ),
    profile: str = typer.Option(
        ...,
        "--profile",
        help="AWS profile (e.g. treadmill-personal) that owns the stack. "
             "No sensible default — each deployment uses its own profile.",
    ),
    region: str = typer.Option(
        "us-east-1",
        "--region",
        help="AWS region. Default is us-east-1 per ADR-0016.",
    ),
    stack_name: str | None = typer.Option(
        None,
        "--stack-name",
        help="CloudFormation stack name. Defaults to "
             "Treadmill<PascalCaseDeploymentId>CloudLite.",
    ),
    output_path: Path | None = typer.Option(
        None,
        "--output-path",
        help="Override the YAML output path. Default is "
             "~/.treadmill/<deployment_id>.yaml.",
    ),
) -> None:
    """Populate ``~/.treadmill/<deployment_id>.yaml`` from CFN outputs.

    Reads the deployed ``TreadmillCloudLite`` stack's CloudFormation
    outputs via ``cloudformation:DescribeStacks`` and writes the per-
    deployment YAML config the API + worker + local-adapter all read.

    Idempotent: re-running overwrites the YAML from current stack state
    (so this is also the post-redeploy "regenerate config" lever).
    """
    resolved_stack = stack_name or _stack_name_for(deployment_id)
    console.print(f"[bold]treadmill-local init {deployment_id}[/bold]")
    console.print(f"  stack:   [cyan]{resolved_stack}[/cyan]")
    console.print(f"  profile: [cyan]{profile}[/cyan]")
    console.print(f"  region:  [cyan]{region}[/cyan]")

    # ── Resolve the AWS account ID via sts:GetCallerIdentity ─────────────────
    # This also asserts that the profile resolves to working credentials
    # before we go any further. If SSO is expired, this is where the
    # operator sees the clear "aws sso login --profile ..." error.
    session = boto3.Session(profile_name=profile, region_name=region)
    try:
        identity = session.client("sts").get_caller_identity()
    except Exception as exc:
        console.print(
            f"[red]sts:GetCallerIdentity failed for profile {profile!r}: "
            f"{exc}[/red]"
        )
        raise typer.Exit(code=1) from exc
    account_id = identity["Account"]
    console.print(f"  account: [cyan]{account_id}[/cyan]")

    # ── Read CFN outputs ─────────────────────────────────────────────────────
    try:
        outputs = read_stack_outputs(
            resolved_stack, profile=profile, region=region,
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    # ── Also read from the observability stack if it exists ───────────────────
    # TreadmillObservabilityStack is optional (deployed with
    # --context include_observability=true). When present, merge its outputs
    # into the combined dict so build_deployment_config populates the
    # aws.observability_* keys. When absent, the CloudLite outputs are used
    # alone and the observability keys are gracefully omitted from the YAML.
    obs_stack_name = _obs_stack_name_for(deployment_id)
    try:
        obs_outputs = read_stack_outputs(
            obs_stack_name, profile=profile, region=region,
        )
        outputs = {**outputs, **obs_outputs}
        console.print(
            f"  [dim]Found observability stack {obs_stack_name} — "
            "merged outputs.[/dim]"
        )
    except ValueError:
        console.print(
            f"  [dim]Observability stack {obs_stack_name} not deployed — "
            "skipping observability outputs.[/dim]"
        )

    # ── Build the YAML-shape dict ────────────────────────────────────────────
    try:
        config = build_deployment_config(
            deployment_id,
            aws_profile=profile,
            aws_region=region,
            aws_account_id=account_id,
            outputs=outputs,
        )
    except KeyError as exc:
        console.print(
            f"[red]CloudFormation outputs are missing a required value: "
            f"{exc.args[0]}[/red]"
        )
        raise typer.Exit(code=1) from exc

    # ── Resolve target path + announce overwrite if applicable ───────────────
    target = (
        Path(output_path).expanduser()
        if output_path is not None
        else Path.home() / ".treadmill" / f"{deployment_id}.yaml"
    )
    if target.exists():
        console.print(
            f"[yellow]• Overwriting existing config at {target}[/yellow]"
        )

    # ── Write ────────────────────────────────────────────────────────────────
    written = write_deployment_yaml(
        deployment_id, config, path=target,
    )
    console.print(f"[green]• Wrote {written}[/green]")
    console.print(
        f"  aws.events_topic_arn:        [dim]{config['aws']['events_topic_arn']}[/dim]"
    )
    console.print(
        f"  aws.work_queue_url:          [dim]{config['aws']['work_queue_url']}[/dim]"
    )
    console.print(
        f"  aws.webhook_api_url:         [dim]{config['aws']['webhook_api_url']}[/dim]"
    )
    console.print(
        f"  secrets.github_pat_secret:   [dim]{config['secrets']['github_pat_secret_name']}[/dim]"
    )


@repo_app.command(name="list")
def repo_list() -> None:
    """List provisioned local bare repos."""
    if not BARE_REPOS_DIR.exists():
        console.print("[dim]no bare repos provisioned yet[/dim]")
        return
    bares = sorted(p for p in BARE_REPOS_DIR.iterdir() if p.is_dir() and p.name.endswith(".git"))
    if not bares:
        console.print("[dim]no bare repos provisioned yet[/dim]")
        return
    for p in bares:
        console.print(f"  {p.name}")


@repo_app.command(name="onboard")
def repo_onboard(
    api_url: str = typer.Option(
        "http://localhost:8088",
        "--api-url",
        help="Base URL of the running Treadmill deployment API.",
    ),
    mode: str | None = typer.Option(
        None,
        "--mode",
        help='Override the onboarding mode ("conform" or "adapt"). When '
             "omitted the server picks via recommend_mode.",
    ),
    allow_auto_merge: bool = typer.Option(
        False,
        "--allow-auto-merge",
        help="Allow auto-merge for this repo. Default is to block — never "
             "auto-merge an external repo unless the operator opts in.",
    ),
) -> None:
    """Onboard the repo in the current working directory (ADR-0051).

    Infers ``owner/name`` from the cwd's git origin remote, builds a
    minimal ``repo_profile`` from the checkout, and POSTs to
    ``{api_url}/api/v1/onboarding/repos``. The server owns the schema
    and ``recommend_mode``; we just hand it a plain dict.
    """
    # The chdir-to-repo-root callback already fired; use the captured
    # invocation cwd so we inspect the operator's target repo, not the
    # Treadmill checkout the CLI lives in.
    root = (_INVOCATION_CWD or Path.cwd()).resolve()
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            check=True, capture_output=True, text=True, cwd=str(root),
        )
    except subprocess.CalledProcessError as exc:
        console.print(
            "[red]could not read origin remote — run this from inside a "
            "git checkout with an 'origin' remote[/red]"
        )
        raise typer.Exit(code=2) from exc

    remote_url = result.stdout.strip()
    try:
        repo = infer_repo(remote_url)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    profile = build_profile(root)
    body = onboard_payload(
        repo,
        profile,
        mode=mode,
        auto_merge_blocked=not allow_auto_merge,
    )

    console.print(f"[bold]treadmill-local repo onboard[/bold]")
    console.print(f"  repo:    [cyan]{repo}[/cyan]")
    console.print(f"  mode:    [cyan]{mode or '(server recommends)'}[/cyan]")
    console.print(f"  api_url: [dim]{api_url}[/dim]")

    url = f"{api_url.rstrip('/')}/api/v1/onboarding/repos"
    try:
        response = httpx.post(url, json=body, timeout=30.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        console.print(f"[red]POST {url} failed: {exc}[/red]")
        raise typer.Exit(code=1) from exc

    data = response.json()
    console.print(f"[green]• onboarded {data.get('repo', repo)}[/green]")
    console.print(f"  resolved mode:       [cyan]{data.get('mode')}[/cyan]")
    console.print(f"  auto_merge_blocked:  [cyan]{data.get('auto_merge_blocked')}[/cyan]")


docs_app = typer.Typer(
    name="docs",
    help="Sync docs between the local mirror and the Treadmill API (ADR-0054).",
    no_args_is_help=True,
)
app.add_typer(docs_app)


def _try_infer_repo_from_cwd() -> str | None:
    """Try to infer owner/name from the cwd's git origin remote."""
    root = (_INVOCATION_CWD or Path.cwd()).resolve()
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            check=True, capture_output=True, text=True, cwd=str(root),
        )
        return infer_repo(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        return None


def _resolve_repo(repo: str | None) -> str:
    resolved = repo or _try_infer_repo_from_cwd()
    if resolved is None:
        console.print(
            "[red]--repo is required (could not infer from cwd git remote)[/red]"
        )
        raise typer.Exit(code=2)
    return resolved


@docs_app.command(name="list")
def docs_list(
    repo: str | None = typer.Option(
        None, "--repo",
        help="Repo as owner/name. Inferred from cwd git remote if omitted.",
    ),
    api_url: str = typer.Option(
        "http://localhost:8088", "--api-url",
        help="Base URL of the Treadmill API.",
    ),
) -> None:
    """List docs for a repo."""
    resolved = _resolve_repo(repo)
    try:
        docs = list_docs(api_url, resolved)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    if not docs:
        console.print("[dim]no docs[/dim]")
        return
    for doc in docs:
        console.print(f"  {doc['doc_path']}  [dim](v{doc['version']})[/dim]")


@docs_app.command(name="get")
def docs_get(
    doc_path: str = typer.Argument(..., help="Doc path (e.g. AGENT.md)."),
    repo: str | None = typer.Option(
        None, "--repo",
        help="Repo as owner/name. Inferred from cwd git remote if omitted.",
    ),
    api_url: str = typer.Option(
        "http://localhost:8088", "--api-url",
        help="Base URL of the Treadmill API.",
    ),
) -> None:
    """Fetch and print a single doc."""
    resolved = _resolve_repo(repo)
    try:
        content = get_doc(api_url, resolved, doc_path)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    console.print(content)


@docs_app.command(name="pull")
def docs_pull(
    repo: str | None = typer.Option(
        None, "--repo",
        help="Repo as owner/name. Inferred from cwd git remote if omitted.",
    ),
    api_url: str = typer.Option(
        "http://localhost:8088", "--api-url",
        help="Base URL of the Treadmill API.",
    ),
    directory: Path = typer.Option(
        Path(".treadmill-docs"), "--dir",
        help="Local mirror directory.",
    ),
) -> None:
    """Sync all docs from the API to a local directory."""
    resolved = _resolve_repo(repo)
    try:
        paths = pull(api_url, resolved, directory)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    for p in paths:
        console.print(f"  pulled: {p}")
    console.print(f"[green]• {len(paths)} doc(s) pulled to {directory}[/green]")


@docs_app.command(name="push")
def docs_push(
    repo: str | None = typer.Option(
        None, "--repo",
        help="Repo as owner/name. Inferred from cwd git remote if omitted.",
    ),
    api_url: str = typer.Option(
        "http://localhost:8088", "--api-url",
        help="Base URL of the Treadmill API.",
    ),
    directory: Path = typer.Option(
        Path(".treadmill-docs"), "--dir",
        help="Local mirror directory.",
    ),
) -> None:
    """Upload docs from a local directory to the API (last-write-wins)."""
    resolved = _resolve_repo(repo)
    try:
        results = push(api_url, resolved, directory)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)
    for doc_path, version in results:
        console.print(f"  pushed: {doc_path} (v{version})")
    console.print(f"[green]• {len(results)} doc(s) pushed[/green]")


if __name__ == "__main__":
    app()
