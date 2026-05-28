"""Onboarding command group — treadmill onboarding update <repo>.

Per ADR-0059 Step 5, this command manages per-repo worker dependency
specs (Python packages, Node packages, signed-URL binaries).
"""

from __future__ import annotations

import re
from typing import Annotated

import typer
from rich.console import Console

from treadmill_cli.api_client import ApiClient, ApiError
from treadmill_cli.config import load_config

onboarding_app = typer.Typer(
    name="onboarding",
    help="Onboarding operations (ADR-0059: per-repo worker deps).",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BINARY_TARGET_PREFIX = "/var/treadmill/repo-bin/"


def _client() -> ApiClient:
    return ApiClient(load_config())


def _handle_api_error(exc: ApiError) -> None:
    err_console.print(f"[red]error {exc.status_code}: {exc.detail}[/red]")
    raise typer.Exit(code=2)


def _parse_binary_spec(spec: str) -> dict:
    """Parse ``name=URL=SHA256@TARGET`` into a BinarySpec dict."""
    if "@" not in spec:
        err_console.print(
            f"[red]invalid --binary spec (missing '@TARGET'): {spec!r}[/red]"
        )
        raise typer.Exit(code=1)
    left, target_path = spec.rsplit("@", 1)
    parts = left.split("=", 2)
    if len(parts) != 3 or not all(parts):
        err_console.print(
            f"[red]invalid --binary spec (expected name=URL=SHA256@TARGET): {spec!r}[/red]"
        )
        raise typer.Exit(code=1)
    name, download_url, sha256_checksum = parts
    if not _SHA256_RE.fullmatch(sha256_checksum):
        err_console.print(
            f"[red]invalid SHA256 in --binary (must be 64 lowercase hex chars): {spec!r}[/red]"
        )
        raise typer.Exit(code=1)
    if not target_path.startswith(_BINARY_TARGET_PREFIX):
        err_console.print(
            f"[red]--binary TARGET must start with {_BINARY_TARGET_PREFIX!r}: {spec!r}[/red]"
        )
        raise typer.Exit(code=1)
    return {
        "name": name,
        "download_url": download_url,
        "sha256_checksum": sha256_checksum,
        "target_path": target_path,
    }


def _merge_worker_deps(
    existing: dict,
    new_python: list[str],
    new_node: list[str],
    new_binaries: list[dict],
) -> dict:
    """Merge new specs into existing worker_deps additively with deduplication."""
    result_python = list(existing.get("python") or [])
    result_node = list(existing.get("node") or [])
    result_binaries = list(existing.get("binaries") or [])

    seen_python = set(result_python)
    for spec in new_python:
        if spec not in seen_python:
            result_python.append(spec)
            seen_python.add(spec)

    seen_node = set(result_node)
    for spec in new_node:
        if spec not in seen_node:
            result_node.append(spec)
            seen_node.add(spec)

    seen_binaries = {
        (b["name"], b["download_url"], b["sha256_checksum"], b["target_path"])
        for b in result_binaries
    }
    for b in new_binaries:
        key = (b["name"], b["download_url"], b["sha256_checksum"], b["target_path"])
        if key not in seen_binaries:
            result_binaries.append(b)
            seen_binaries.add(key)

    return {"python": result_python, "node": result_node, "binaries": result_binaries}


@onboarding_app.command("update")
def onboarding_update(
    repo: Annotated[str, typer.Argument(help="org/repo slug.")],
    worker_deps_python: Annotated[list[str] | None, typer.Option(
        "--worker-deps-python",
        help="Python package spec to add (repeatable).",
    )] = None,
    worker_deps_node: Annotated[list[str] | None, typer.Option(
        "--worker-deps-node",
        help="Node package spec to add (repeatable).",
    )] = None,
    binary: Annotated[list[str] | None, typer.Option(
        "--binary",
        help="Binary spec: name=URL=SHA256@TARGET (repeatable).",
    )] = None,
    clear_worker_deps: Annotated[bool, typer.Option(
        "--clear-worker-deps",
        help="Empty all worker dependency lists (mutually exclusive with dep flags).",
    )] = False,
) -> None:
    """Update per-repo worker dependencies (ADR-0059 Step 5).

    Without ``--clear-worker-deps``, appends specs to the existing lists
    (additive, deduplicated by exact string). With ``--clear-worker-deps``,
    all three lists are replaced with empty lists.
    """
    any_deps = bool(worker_deps_python or worker_deps_node or binary)
    if clear_worker_deps and any_deps:
        err_console.print(
            "[red]--clear-worker-deps cannot be combined with "
            "--worker-deps-python, --worker-deps-node, or --binary[/red]"
        )
        raise typer.Exit(code=1)

    parsed_binaries: list[dict] = [
        _parse_binary_spec(spec) for spec in (binary or [])
    ]

    try:
        with _client() as client:
            try:
                current = client.get_repo_config(repo)
            except ApiError as exc:
                if exc.status_code == 404:
                    err_console.print(
                        f"[red]repo {repo!r} is not registered — "
                        f"run `treadmill onboarding add` first[/red]"
                    )
                    raise typer.Exit(code=1)
                raise

            if clear_worker_deps:
                merged: dict = {"python": [], "node": [], "binaries": []}
            else:
                merged = _merge_worker_deps(
                    current.get("worker_deps") or {},
                    list(worker_deps_python or []),
                    list(worker_deps_node or []),
                    parsed_binaries,
                )

            post_body: dict = {
                "repo": repo,
                "profile": {"repo": repo},
                "mode": current.get("mode", "conform"),
                "auto_merge_blocked": current.get("auto_merge_blocked", False),
                "claude_account": current.get("claude_account"),
                "worker_deps": merged,
            }

            try:
                client.upsert_repo_config(post_body)
            except ApiError as exc:
                if exc.status_code == 422:
                    err_console.print(f"[red]validation error: {exc.detail}[/red]")
                    raise typer.Exit(code=1)
                raise

    except ApiError as exc:
        _handle_api_error(exc)

    n_py = len(merged["python"])
    n_node = len(merged["node"])
    n_bin = len(merged["binaries"])
    console.print(
        f"updated worker_deps for {repo}: python={n_py} node={n_node} binaries={n_bin}"
    )
