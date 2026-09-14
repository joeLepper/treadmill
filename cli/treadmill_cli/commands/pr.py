"""PR-state poll reconciler CLI (ADR-0113).

``treadmill pr poll <repo> --account <acct>`` reconciles a WEBHOOKLESS repo: it
reads the repo's OPEN ``task_prs`` (the poll set) from the API, checks each PR's
merge + CI state on GitHub with the repo's own collaborator credential, and calls
the ``poll-ingest`` endpoints on a new transition. The endpoints synthesize the
same events a webhook would, routed through the shared seam, so the coordinator
pipeline is unchanged (see ADR-0113).

Two hard rules from the operator's Netlify scoping directive:

* The poller authenticates with ``GH_TOKEN=$(gh auth token --user <account>)`` per
  gh call. It NEVER runs ``gh auth switch`` (that drifts the host's active account
  every session). The token is read once and passed in the subprocess env; it is
  never logged.
* Fail-closed: on ANY gh non-zero the poller emits NOTHING for that PR/leg. A
  failed read must never synthesize a false merge or CI state. A broken token
  aborts the whole poll (still emitting nothing).

Single-flight: a per-repo host flock serializes overlapping invocations (e.g. two
timer ticks). The ``poll-ingest`` existence-gate is SELECT-then-seam and NOT atomic,
so two concurrent pollers for the same ``(repo, pr, anchor)`` could both miss and
both publish (Ernie, ADR-0113 review). One poller per repo removes that race.
"""

from __future__ import annotations

import contextlib
import fcntl
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from treadmill_cli.api_client import ApiClient, ApiError
from treadmill_cli.config import load_config

pr_app = typer.Typer(name="pr", help="PR-state reconciliation.", no_args_is_help=True)

out = Console()
err = Console(stderr=True)

_POLL_LOCK_DIR = Path.home() / ".treadmill"

# The gh runner seam — a single subprocess wrapper both parsers use, injectable so
# foils can drive the poller without a real GitHub. Returns the CompletedProcess.
GhRunner = Callable[[list[str], "str | None"], "subprocess.CompletedProcess[str]"]


class GhError(RuntimeError):
    """A gh invocation returned non-zero. Fail-closed: the caller emits nothing."""


def _real_run_gh(args: list[str], token: str | None) -> subprocess.CompletedProcess[str]:
    """Run ``gh <args>`` with the account token in GH_TOKEN. The token is passed in
    the env and never logged. NEVER runs ``gh auth switch``."""
    import os

    env = dict(os.environ)
    if token is not None:
        env["GH_TOKEN"] = token
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, env=env, check=False,
    )


def _gh_token(account: str, *, run_gh: GhRunner) -> str:
    """Read the account's stored gh token (``gh auth token --user <account>``). The
    returned value is a credential — the caller must never log it. Raises GhError on
    a non-zero exit so a broken credential fails the poll closed."""
    proc = run_gh(["auth", "token", "--user", account], None)
    if proc.returncode != 0:
        # Do NOT include stdout — it could carry a partial token.
        raise GhError(f"gh auth token --user {account} failed (exit {proc.returncode})")
    token = (proc.stdout or "").strip()
    if not token:
        raise GhError(f"gh auth token --user {account} returned an empty token")
    return token


def _gh_json(args: list[str], token: str, *, run_gh: GhRunner) -> Any:
    """Run a gh command expected to print JSON; parse + return it. Raises GhError on
    a non-zero exit (fail-closed) or unparseable output."""
    import json

    proc = run_gh(args, token)
    if proc.returncode != 0:
        raise GhError(f"gh {' '.join(args)} failed (exit {proc.returncode})")
    try:
        return json.loads(proc.stdout or "")
    except json.JSONDecodeError as exc:
        raise GhError(f"gh {' '.join(args)} returned non-JSON output") from exc


def _slug(repo: str) -> str:
    return repo.replace("/", "-").lower()


@contextlib.contextmanager
def _poll_lock(repo: str, *, blocking: bool = False) -> Iterator[bool]:
    """Per-repo single-flight lock. Non-blocking by default: a concurrent tick for
    the SAME repo yields False (skip) rather than racing. Different repos have
    different lock files, so their timers run concurrently."""
    _POLL_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _POLL_LOCK_DIR / f"pr-poll-{_slug(repo)}.lock"
    fh = open(lock_path, "w")
    flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        try:
            fcntl.flock(fh, flags)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


@dataclass
class PollSummary:
    """What one poll pass did — the CLI prints it; foils assert on it."""

    polled: int = 0
    merges_ingested: int = 0
    ci_ingested: int = 0
    skipped_gh_errors: int = 0
    notes: list[str] = field(default_factory=list)


def run_poll(
    repo: str,
    account: str,
    client: ApiClient,
    *,
    run_gh: GhRunner = _real_run_gh,
) -> PollSummary:
    """Reconcile one webhookless repo's open PRs. Pure logic (the flock is the
    caller's) so foils can drive it with a fake client + fake run_gh.

    Fail-closed: a per-PR gh error skips that PR (emits nothing) and is counted; a
    token error raises (the whole poll emits nothing). Idempotent: the ingest
    endpoints dedup, so re-polling an already-recorded merge/CI is a server no-op."""
    summary = PollSummary()
    token = _gh_token(account, run_gh=run_gh)  # raises → poll aborts, emits nothing

    open_prs = client.list_open_task_prs(repo)
    for pr in open_prs:
        pr_number = int(pr["pr_number"])
        summary.polled += 1
        try:
            view = _gh_json(
                [
                    "pr", "view", str(pr_number), "--repo", repo,
                    "--json", "mergeCommitOid,headRefOid,merged,state",
                ],
                token,
                run_gh=run_gh,
            )
        except GhError:
            # Fail-closed: a failed read emits NOTHING for this PR — never a false state.
            summary.skipped_gh_errors += 1
            summary.notes.append(f"pr #{pr_number}: gh pr view failed; skipped")
            continue

        merged = bool(view.get("merged"))
        merge_sha = view.get("mergeCommitOid") or None
        head_sha = view.get("headRefOid") or None

        # MERGE leg — a PR merges once; the endpoint dedups on the merge sha.
        if merged and merge_sha:
            resp = client.poll_ingest_merge(
                repo=repo, pr_number=pr_number, merge_sha=merge_sha,
            )
            if not resp.get("already_ingested"):
                summary.merges_ingested += 1
                summary.notes.append(f"pr #{pr_number}: merge ingested")

        # CI leg — read the head's check SUITES; ingest each COMPLETED suite. The
        # endpoint keys on (suite_id, head_sha, conclusion), so a re-poll is a no-op
        # and a re-run whose conclusion changed re-emits.
        if head_sha:
            try:
                suites_doc = _gh_json(
                    ["api", f"repos/{repo}/commits/{head_sha}/check-suites"],
                    token,
                    run_gh=run_gh,
                )
            except GhError:
                summary.skipped_gh_errors += 1
                summary.notes.append(f"pr #{pr_number}: check-suites read failed; skipped")
                continue
            for suite in suites_doc.get("check_suites", []) or []:
                if suite.get("status") != "completed":
                    continue  # netlify's eternal 'queued', in-progress suites → skip
                conclusion = suite.get("conclusion")
                suite_id = suite.get("id")
                if not conclusion or suite_id is None:
                    continue
                app_slug = (suite.get("app") or {}).get("slug") or ""
                resp = client.poll_ingest_check_run(
                    repo=repo,
                    head_sha=head_sha,
                    check_suite_id=int(suite_id),
                    conclusion=str(conclusion),
                    app_slug=app_slug,
                    pr_number=pr_number,
                )
                if not resp.get("already_ingested"):
                    summary.ci_ingested += 1
                    summary.notes.append(
                        f"pr #{pr_number}: ci ingested (suite {suite_id} {conclusion})"
                    )
    return summary


@pr_app.command("poll")
def poll(
    repo: Annotated[
        str,
        typer.Argument(help="Repo in ``owner/name`` form."),
    ],
    account: Annotated[
        str,
        typer.Option(
            "--account",
            help=(
                "gh account whose collaborator credential reads this repo (e.g. "
                "``joelepper-netlify``). The poller uses "
                "``GH_TOKEN=$(gh auth token --user <account>)`` — it NEVER switches "
                "the active gh account."
            ),
        ),
    ],
) -> None:
    """Reconcile a webhookless repo's open PRs — synthesize merge + CI events for
    the ones GitHub shows merged/CI-complete (ADR-0113). Single-flighted per repo."""
    with _poll_lock(repo, blocking=False) as acquired:
        if not acquired:
            err.print(
                f"[yellow]pr poll: another poll for {repo} is running; skipping.[/yellow]"
            )
            raise typer.Exit(code=0)
        try:
            with ApiClient(load_config()) as client:
                summary = run_poll(repo, account, client)
        except GhError as exc:
            err.print(f"[red]pr poll: {exc} — poll aborted (emitted nothing).[/red]")
            raise typer.Exit(code=1) from exc
        except ApiError as exc:
            err.print(f"[red]pr poll: API error: {exc} — poll aborted.[/red]")
            raise typer.Exit(code=1) from exc

    out.print(
        f"pr poll {repo}: polled {summary.polled}, "
        f"merges +{summary.merges_ingested}, ci +{summary.ci_ingested}, "
        f"skipped(gh-error) {summary.skipped_gh_errors}"
    )
    for note in summary.notes:
        out.print(f"  {note}")
