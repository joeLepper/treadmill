"""Tokens command group — ``treadmill tokens harvest`` / ``report``.

ADR-0089 §2: the ``llm_calls`` table is the standing token meter, and
this module is its writer + reader.

``harvest`` walks a Claude Code projects dir (default
``~/.claude/projects``), parses every session transcript JSONL beyond
the per-file byte cursor the API remembers, and posts the extracted
calls to ``POST /api/v1/llm_calls/harvest``:

* One LLM call streams **multiple** transcript lines (one per content
  block) sharing a ``requestId`` and repeating the usage object — the
  parser keeps the last line per requestId within the parsed span.
* The byte cursor only ever advances past *complete* lines; a partial
  trailing line waits for the next run. A response still streaming when
  the file is snapshotted can straddle the cursor — the re-sent call
  hits the API's (transcript_path, request_id) unique index and UPDATES
  the earlier row's usage in place (last-write-wins, the cross-run
  analogue of the in-span last-line-wins rule: the first write was a
  mid-stream undercount). Paths are canonicalized (``resolve()``) so a
  path-spelling change can't fork the cursor/index keys.
* Unparseable lines are never silently skipped (ADR-0089): each run
  counts them and sends the cumulative per-file total (which the API
  overwrites — retry-idempotent); both ``harvest`` output and ``report``
  surface them.

Attribution is two-step: the transcript's project dir name yields the
session label (team sessions live under
``~/.treadmill/teams/<team>/<label>``, whose munged dir name ends with
the label; anything else keeps the raw dir name as its label), then
each call joins to a ``task_executions`` window (worker_label +
started_at..completed_at containing called_at) when **exactly one**
window matches — zero or ambiguous matches leave ``task_execution_id``
NULL, the honest shape for orchestrator/coordinator sessions that have
no dispatch cycle (see the nullable-FK decision in
``services/api/treadmill_api/models/llm_call.py``).

``report --since`` renders the API's per-label rollup: calls, output,
fresh input, cache creation/read, cache-hit ratio, malformed-line total.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from treadmill_cli.api_client import ApiClient, ApiError
from treadmill_cli.config import load_config

tokens_app = typer.Typer(
    name="tokens",
    help="Token-economics meter (ADR-0089: harvest transcripts, report burn).",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)

# Rightmost role-prefixed segment of a munged project-dir name is the
# session label: ``-home-joe--treadmill-teams-<team>-worker-<team>-2``
# → ``worker-<team>-2``. The trailing hyphen requirement keeps
# ``-workers-agent`` (worktree dirs) from matching.
_ROLE_LABEL_RE = re.compile(r"-(?=(?:worker|coordinator|evaluator|orchestrator)-)")


def _client() -> ApiClient:
    return ApiClient(load_config())


def _handle_api_error(exc: ApiError) -> None:
    err_console.print(f"[red]error {exc.status_code}: {exc.detail}[/red]")
    raise typer.Exit(code=2)


def attribute_label(project_dir_name: str) -> str:
    """Session label for a ``~/.claude/projects/<name>`` directory."""
    matches = list(_ROLE_LABEL_RE.finditer(project_dir_name))
    if matches:
        return project_dir_name[matches[-1].start() + 1 :]
    return project_dir_name


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass
class TranscriptSpan:
    """Result of parsing one transcript file beyond its byte cursor."""

    new_offset: int
    malformed: int = 0
    # requestId → extracted call dict; last line per requestId wins.
    calls: dict[str, dict[str, Any]] = field(default_factory=dict)


def parse_transcript_span(path: Path, start_offset: int) -> TranscriptSpan:
    """Extract per-call usage from ``path`` starting at ``start_offset``.

    Consumes only complete (newline-terminated) lines. A line is
    *malformed* when it is not JSON, or when it is an assistant line
    carrying a usage object whose call fields (requestId / timestamp /
    model / token counts) cannot be extracted. Valid non-usage lines
    (user turns, summaries, …) are simply not calls.
    """
    with path.open("rb") as fh:
        fh.seek(start_offset)
        data = fh.read()
    last_newline = data.rfind(b"\n")
    if last_newline == -1:
        return TranscriptSpan(new_offset=start_offset)
    span = TranscriptSpan(new_offset=start_offset + last_newline + 1)

    for raw_line in data[: last_newline + 1].split(b"\n"):
        if not raw_line.strip():
            continue
        try:
            obj = json.loads(raw_line)
        except (ValueError, UnicodeDecodeError):
            span.malformed += 1
            continue
        if not isinstance(obj, dict):
            span.malformed += 1
            continue
        if obj.get("type") != "assistant":
            continue
        message = obj.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            continue
        call = _extract_call(obj, message, usage)
        if call is None:
            span.malformed += 1
            continue
        span.calls[call["request_id"]] = call
    return span


def _extract_call(
    obj: dict[str, Any], message: dict[str, Any], usage: dict[str, Any]
) -> dict[str, Any] | None:
    request_id = obj.get("requestId") or message.get("id")
    called_at = _parse_timestamp(obj.get("timestamp"))
    model = message.get("model")
    if not isinstance(request_id, str) or called_at is None or not isinstance(model, str):
        return None
    try:
        return {
            "request_id": request_id,
            "called_at": called_at,
            "model": model,
            "input_tokens": int(usage.get("input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0)),
            "cache_creation_tokens": int(usage.get("cache_creation_input_tokens", 0)),
            "cache_read_tokens": int(usage.get("cache_read_input_tokens", 0)),
        }
    except (TypeError, ValueError):
        return None


def match_execution(
    executions: list[dict[str, Any]], called_at: datetime
) -> str | None:
    """Execution id whose started_at..completed_at window contains the call.

    Exactly-one semantics per ADR-0089: zero matches (no dispatch cycle —
    orchestrator/coordinator sessions) and ambiguous matches both yield
    None rather than a guess.
    """
    matches = []
    for execution in executions:
        started_at = _parse_timestamp(execution.get("started_at"))
        if started_at is None or called_at < started_at:
            continue
        completed_at = _parse_timestamp(execution.get("completed_at"))
        if completed_at is not None and called_at > completed_at:
            continue
        matches.append(execution)
    if len(matches) == 1:
        return str(matches[0]["id"])
    return None


# ── Commands ─────────────────────────────────────────────────────────────


@tokens_app.command("harvest")
def harvest(
    projects_dir: Annotated[
        Path,
        typer.Option(
            "--projects-dir",
            help="Claude Code projects dir holding session transcript JSONL.",
        ),
    ] = Path.home() / ".claude" / "projects",
) -> None:
    """Parse new transcript spans into llm_calls (idempotent re-runs)."""
    if not projects_dir.is_dir():
        err_console.print(f"[red]not a directory: {projects_dir}[/red]")
        raise typer.Exit(code=2)
    # Canonicalize: the cursor key AND the (transcript_path, request_id)
    # unique index both key on this string — a relative path, symlinked
    # home, or cron-vs-interactive spelling difference would defeat both
    # idempotency layers at once and double-count every call.
    projects_dir = projects_dir.resolve()

    files_harvested = 0
    inserted = 0
    updated = 0
    malformed = 0
    executions_by_label: dict[str, list[dict[str, Any]]] = {}

    try:
        with _client() as client:
            cursors = {
                c["transcript_path"]: c for c in client.list_harvest_cursors()
            }
            for transcript in sorted(projects_dir.glob("*/*.jsonl")):
                transcript_path = str(transcript)
                cursor = cursors.get(transcript_path)
                start_offset = cursor["byte_offset"] if cursor else 0
                prior_malformed = cursor["malformed_lines"] if cursor else 0
                if transcript.stat().st_size <= start_offset:
                    continue
                span = parse_transcript_span(transcript, start_offset)
                if span.new_offset <= start_offset and not span.malformed:
                    continue

                label = attribute_label(transcript.parent.name)
                if span.calls and label not in executions_by_label:
                    executions_by_label[label] = client.list_task_executions(
                        worker_label=label
                    )
                calls_payload = [
                    {
                        **call,
                        "called_at": call["called_at"].isoformat(),
                        "session_label": label,
                        "task_execution_id": match_execution(
                            executions_by_label.get(label, []), call["called_at"]
                        ),
                    }
                    for call in span.calls.values()
                ]
                result = client.harvest_llm_calls(
                    transcript_path=transcript_path,
                    byte_offset=span.new_offset,
                    # Cumulative per-file count: the server overwrites this
                    # absolute value, so re-sending the same span (lost
                    # response, retry) cannot inflate the metric.
                    malformed_lines=prior_malformed + span.malformed,
                    calls=calls_payload,
                )
                files_harvested += 1
                inserted += result["inserted"]
                updated += result["updated"]
                malformed += span.malformed
    except ApiError as exc:
        _handle_api_error(exc)

    console.print(
        f"harvested {files_harvested} transcript(s): "
        f"{inserted} call(s) inserted, {updated} straddled call(s) updated, "
        f"{malformed} malformed line(s) counted"
    )


@tokens_app.command("report")
def report(
    since: Annotated[
        str,
        typer.Option(
            "--since",
            help="ISO date or datetime; rollup covers calls at/after this instant.",
        ),
    ],
) -> None:
    """Per-label token rollup from harvested llm_calls rows."""
    try:
        since_dt = datetime.fromisoformat(since)
    except ValueError:
        err_console.print(f"[red]--since must be an ISO date/datetime: {since!r}[/red]")
        raise typer.Exit(code=2)
    if since_dt.tzinfo is None:
        since_dt = since_dt.replace(tzinfo=timezone.utc)

    try:
        with _client() as client:
            data = client.token_report(since=since_dt.isoformat())
    except ApiError as exc:
        _handle_api_error(exc)

    table = Table(title=f"Token report since {since_dt.isoformat()}")
    table.add_column("label")
    table.add_column("calls", justify="right")
    table.add_column("output", justify="right")
    table.add_column("fresh-in", justify="right")
    table.add_column("cache-creation", justify="right")
    table.add_column("cache-read", justify="right")
    table.add_column("hit-ratio", justify="right")
    for row in data["rows"]:
        table.add_row(
            row["session_label"],
            f"{row['calls']:,}",
            f"{row['output_tokens']:,}",
            f"{row['input_tokens']:,}",
            f"{row['cache_creation_tokens']:,}",
            f"{row['cache_read_tokens']:,}",
            f"{row['cache_hit_ratio']:.1%}",
        )
    console.print(table)
    # ADR-0089: unparseable transcript lines are counted and REPORTED.
    console.print(
        f"malformed transcript lines counted: {data['malformed_lines_total']}"
    )
