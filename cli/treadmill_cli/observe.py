"""Observe command group — treadmill observe.

Per ADR-0020 §"CLI as the access layer: `treadmill observe`", this module
provides a read-only access layer to the deployment's Grafana stack.

Subcommands:
  treadmill observe dashboard [--name <dashboard>]
  treadmill observe logs --task <task-id>
  treadmill observe traces --task <task-id>
  treadmill observe metrics --metric <metric-name>
  treadmill observe status
  treadmill observe open {dashboard|logs|traces|metrics} [args]
"""

from __future__ import annotations

import json
import socket
import subprocess
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.console import Console

observe_app = typer.Typer(
    name="observe",
    help="Observability access layer — open Grafana panels (ADR-0020).",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)

# Datasource UIDs — constants per ADR-0020 §"Datasource UIDs are constants"
_LOKI_UID = "loki"
_PROMETHEUS_UID = "prometheus"
_TEMPO_UID = "tempo"

_DEFAULT_DASHBOARD = "treadmill-overview"
_GRAFANA_PORT = 3000
_SSM_LOCAL_PORT = 3000
_DIRECT_TIMEOUT = 2.0  # seconds


# ── Deployment config ─────────────────────────────────────────────────────────


def _load_obs_config(deployment_id: str) -> dict[str, Any]:
    """Load the aws block from ~/.treadmill/<deployment_id>.yaml.

    Exits with code 2 when the file is missing, malformed, or the
    observability stack outputs are absent.
    """
    path = Path.home() / ".treadmill" / f"{deployment_id}.yaml"
    if not path.exists():
        err_console.print(
            f"[red]deployment config not found at {path}; run "
            f"`treadmill-local init {deployment_id} --profile <profile>` "
            f"to create it.[/red]"
        )
        raise typer.Exit(code=2)

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        err_console.print(f"[red]failed to parse {path}: {exc}[/red]")
        raise typer.Exit(code=2)

    if not isinstance(raw, dict):
        err_console.print(f"[red]deployment config at {path} is not a YAML mapping[/red]")
        raise typer.Exit(code=2)

    aws = raw.get("aws") or {}
    if not aws.get("observability_grafana_host"):
        err_console.print(
            f"[red]deployment config at {path} has no observability_grafana_host; "
            f"the TreadmillObservabilityStack may not be deployed. "
            f"Re-run `treadmill-local init {deployment_id}` after deploying it.[/red]"
        )
        raise typer.Exit(code=2)

    return aws


# ── Reachability + SSM tunnel ─────────────────────────────────────────────────


def check_direct_reachable(host: str, port: int = _GRAFANA_PORT) -> bool:
    """Return True if host:port accepts a TCP connection within the timeout."""
    try:
        with socket.create_connection((host, port), timeout=_DIRECT_TIMEOUT):
            return True
    except (OSError, TimeoutError):
        return False


def start_ssm_tunnel(
    ec2_id: str,
    remote_host: str,
    remote_port: int = _GRAFANA_PORT,
    local_port: int = _SSM_LOCAL_PORT,
) -> subprocess.Popen:
    """Start an SSM port-forwarding session as a background subprocess."""
    params = json.dumps({
        "host": [remote_host],
        "portNumber": [str(remote_port)],
        "localPortNumber": [str(local_port)],
    })
    cmd = [
        "aws", "ssm", "start-session",
        "--target", ec2_id,
        "--document-name", "AWS-StartPortForwardingSessionToRemoteHost",
        "--parameters", params,
    ]
    console.print(
        f"[dim]SSM tunnel: localhost:{local_port} → {remote_host}:{remote_port}[/dim]"
    )
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _resolve_grafana_base_url(aws: dict[str, Any]) -> tuple[str, subprocess.Popen | None]:
    """Determine the Grafana base URL; start SSM tunnel if direct access fails.

    Returns (base_url, ssm_proc_or_None). Caller is responsible for
    waiting on / terminating ssm_proc when the session ends.
    """
    grafana_host = aws["observability_grafana_host"]
    ec2_id = aws.get("observability_ec2_id")

    if check_direct_reachable(grafana_host, _GRAFANA_PORT):
        return f"http://{grafana_host}:{_GRAFANA_PORT}", None

    if not ec2_id:
        err_console.print(
            f"[red]Grafana at {grafana_host}:{_GRAFANA_PORT} is unreachable "
            f"and no observability_ec2_id is configured for SSM fallback.[/red]"
        )
        raise typer.Exit(code=2)

    proc = start_ssm_tunnel(ec2_id, grafana_host, _GRAFANA_PORT, _SSM_LOCAL_PORT)
    return f"http://localhost:{_SSM_LOCAL_PORT}", proc


def _wait_for_tunnel(proc: subprocess.Popen) -> None:
    """Block until the SSM tunnel process exits; terminate on KeyboardInterrupt."""
    console.print("[dim]SSM tunnel active — press Ctrl-C to close.[/dim]")
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()


# ── URL construction ──────────────────────────────────────────────────────────


def build_explore_url(base_url: str, datasource_uid: str, query_key: str, query: str) -> str:
    """Build a Grafana Explore URL for the given datasource and query."""
    left = {
        "datasource": datasource_uid,
        "queries": [{"refId": "A", query_key: query}],
        "range": {"from": "now-1h", "to": "now"},
    }
    encoded = urllib.parse.quote(json.dumps(left))
    return f"{base_url}/explore?orgId=1&left={encoded}"


def loki_url(base_url: str, task_id: str) -> str:
    return build_explore_url(base_url, _LOKI_UID, "expr", f'{{task_id="{task_id}"}}')


def tempo_url(base_url: str, task_id: str) -> str:
    return build_explore_url(
        base_url, _TEMPO_UID, "search",
        f"resource.attributes.task_id={task_id}",
    )


def prometheus_url(base_url: str, metric: str) -> str:
    return build_explore_url(base_url, _PROMETHEUS_UID, "expr", metric)


def dashboard_url(base_url: str, name: str) -> str:
    return f"{base_url}/d/{name}"


# ── Browser helper ────────────────────────────────────────────────────────────


def _open_browser(url: str) -> None:
    """Open url in operator's default browser; fall back to printing the URL."""
    if not webbrowser.open(url):
        console.print(f"[yellow]Could not open browser. Navigate to:[/yellow] {url}")
    else:
        console.print(f"[dim]{url}[/dim]")


def _open_with_tunnel(aws: dict[str, Any], url_fn) -> None:
    """Resolve base URL (starting SSM tunnel if needed), open in browser."""
    base_url, proc = _resolve_grafana_base_url(aws)
    _open_browser(url_fn(base_url))
    if proc:
        _wait_for_tunnel(proc)


# ── Subcommands ───────────────────────────────────────────────────────────────


_DEPLOYMENT_OPTION = typer.Option(
    ...,
    "--deployment", "-d",
    envvar="TREADMILL_DEPLOYMENT_ID",
    help="Deployment ID (or set $TREADMILL_DEPLOYMENT_ID).",
)


@observe_app.command("dashboard")
def obs_dashboard(
    deployment: Annotated[str, _DEPLOYMENT_OPTION],
    name: Annotated[str, typer.Option(
        "--name", "-n", help="Dashboard name/UID.",
    )] = _DEFAULT_DASHBOARD,
) -> None:
    """Open a Grafana dashboard in the operator's browser."""
    aws = _load_obs_config(deployment)
    _open_with_tunnel(aws, lambda base: dashboard_url(base, name))


@observe_app.command("logs")
def obs_logs(
    deployment: Annotated[str, _DEPLOYMENT_OPTION],
    task: Annotated[str, typer.Option("--task", "-t", help="Task ID to filter by.")],
) -> None:
    """Open Grafana Explore with Loki logs filtered by task ID."""
    aws = _load_obs_config(deployment)
    _open_with_tunnel(aws, lambda base: loki_url(base, task))


@observe_app.command("traces")
def obs_traces(
    deployment: Annotated[str, _DEPLOYMENT_OPTION],
    task: Annotated[str, typer.Option("--task", "-t", help="Task ID to filter by.")],
) -> None:
    """Open Grafana Explore with Tempo traces filtered by task ID."""
    aws = _load_obs_config(deployment)
    _open_with_tunnel(aws, lambda base: tempo_url(base, task))


@observe_app.command("metrics")
def obs_metrics(
    deployment: Annotated[str, _DEPLOYMENT_OPTION],
    metric: Annotated[str, typer.Option("--metric", "-m", help="Prometheus metric name.")],
) -> None:
    """Open Grafana Explore with Prometheus metrics."""
    aws = _load_obs_config(deployment)
    _open_with_tunnel(aws, lambda base: prometheus_url(base, metric))


@observe_app.command("status")
def obs_status(
    deployment: Annotated[str, _DEPLOYMENT_OPTION],
) -> None:
    """Check Grafana reachability and report access method without opening a browser."""
    aws = _load_obs_config(deployment)
    grafana_host = aws["observability_grafana_host"]
    ec2_id = aws.get("observability_ec2_id")

    if check_direct_reachable(grafana_host, _GRAFANA_PORT):
        console.print(
            f"[green]reachable[/green] http://{grafana_host}:{_GRAFANA_PORT}"
        )
        console.print("  access: direct")
        return

    if ec2_id:
        console.print(
            f"[yellow]not directly reachable[/yellow] {grafana_host}:{_GRAFANA_PORT}"
        )
        console.print(f"  access: SSM tunnel via {ec2_id}")
        ssm_params = json.dumps({
            "host": [grafana_host],
            "portNumber": ["3000"],
            "localPortNumber": ["3000"],
        })
        console.print(
            f"  command: aws ssm start-session "
            f"--target {ec2_id} "
            f"--document-name AWS-StartPortForwardingSessionToRemoteHost "
            f"--parameters '{ssm_params}'"
        )
    else:
        err_console.print(
            f"[red]not reachable[/red] {grafana_host}:{_GRAFANA_PORT} "
            f"(no observability_ec2_id for SSM fallback)"
        )
        raise typer.Exit(code=2)


_OPEN_TARGETS = ("dashboard", "logs", "traces", "metrics")


@observe_app.command("open")
def obs_open(
    target: Annotated[str, typer.Argument(
        help=f"What to open: {', '.join(_OPEN_TARGETS)}.",
    )],
    deployment: Annotated[str, _DEPLOYMENT_OPTION],
    task: Annotated[str | None, typer.Option(
        "--task", "-t", help="Task ID (required for logs/traces).",
    )] = None,
    metric: Annotated[str | None, typer.Option(
        "--metric", "-m", help="Metric name (required for metrics).",
    )] = None,
    name: Annotated[str, typer.Option(
        "--name", "-n", help="Dashboard name/UID (for dashboard).",
    )] = _DEFAULT_DASHBOARD,
) -> None:
    """Construct and print a Grafana URL without opening a browser or SSM tunnel.

    Useful for runbooks and scripted access. The URL uses the deployment's
    direct Grafana host — no reachability check is performed.
    """
    if target not in _OPEN_TARGETS:
        err_console.print(
            f"[red]unknown target {target!r}; must be one of: "
            f"{', '.join(_OPEN_TARGETS)}[/red]"
        )
        raise typer.Exit(code=2)

    aws = _load_obs_config(deployment)
    base_url = f"http://{aws['observability_grafana_host']}:{_GRAFANA_PORT}"

    if target == "dashboard":
        url = dashboard_url(base_url, name)
    elif target == "logs":
        if not task:
            err_console.print("[red]--task is required for 'logs'[/red]")
            raise typer.Exit(code=2)
        url = loki_url(base_url, task)
    elif target == "traces":
        if not task:
            err_console.print("[red]--task is required for 'traces'[/red]")
            raise typer.Exit(code=2)
        url = tempo_url(base_url, task)
    else:  # metrics
        if not metric:
            err_console.print("[red]--metric is required for 'metrics'[/red]")
            raise typer.Exit(code=2)
        url = prometheus_url(base_url, metric)

    console.print(url)
