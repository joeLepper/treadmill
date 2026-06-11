"""Tests for ``treadmill tokens harvest`` / ``report`` (ADR-0089 §2).

Fixture-JSONL coverage axes from the implementation plan:

* happy path — usage lines parsed, deduped per requestId, label
  attributed from the project-dir name, posted with the cursor offset
* idempotent rerun — a cursor at EOF means no harvest POST at all
* malformed lines — counted into ``malformed_lines_delta`` and surfaced
  in command output, never silently skipped
* window-join attribution — exactly-one execution window match sets
  ``task_execution_id``; zero or ambiguous matches leave it NULL
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from treadmill_cli.cli import app
from treadmill_cli.commands.tokens import (
    attribute_label,
    match_execution,
    parse_transcript_span,
)


runner = CliRunner()

API = "http://fake-api"
TEAM_DIR = "-home-x--treadmill-teams-team1-worker-team1-1"
LABEL = "worker-team1-1"


@pytest.fixture(autouse=True)
def _api_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TREADMILL_API_URL", API)


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin a wide console so rich never wraps/truncates asserted text."""
    from rich.console import Console

    import treadmill_cli.commands.tokens as tokens_mod

    monkeypatch.setattr(tokens_mod, "console", Console(width=300))


def _plain(output: str) -> str:
    """Whitespace-normalized output — immune to console line wrapping."""
    return " ".join(output.split())


def _usage_line(
    request_id: str,
    *,
    timestamp: str = "2026-06-10T12:00:00.000Z",
    model: str = "claude-fable-5",
    input_tokens: int = 100,
    output_tokens: int = 10,
    cache_creation: int = 1_000,
    cache_read: int = 50_000,
) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "requestId": request_id,
            "timestamp": timestamp,
            "message": {
                "id": f"msg_{request_id}",
                "model": model,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_creation_input_tokens": cache_creation,
                    "cache_read_input_tokens": cache_read,
                    "service_tier": "standard",
                },
            },
        }
    )


def _user_line() -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}})


def _write_transcript(
    projects_dir: Path, lines: list[str], *, dir_name: str = TEAM_DIR
) -> Path:
    session_dir = projects_dir / dir_name
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "abc123.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


def _execution(
    *,
    worker_label: str = LABEL,
    started_at: str = "2026-06-10T11:00:00+00:00",
    completed_at: str | None = "2026-06-10T13:00:00+00:00",
) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "task_id": str(uuid.uuid4()),
        "worker_label": worker_label,
        "trigger": "initial",
        "status": "completed" if completed_at else "running",
        "failure_reason": None,
        "started_at": started_at,
        "completed_at": completed_at,
    }


def _mock_cursors(httpx_mock: HTTPXMock, cursors: list[dict]) -> None:
    httpx_mock.add_response(
        url=f"{API}/api/v1/llm_calls/harvest_cursors", json=cursors
    )


def _mock_executions(httpx_mock: HTTPXMock, executions: list[dict]) -> None:
    httpx_mock.add_response(
        url=f"{API}/api/v1/task_executions?worker_label={LABEL}", json=executions
    )


def _mock_harvest(httpx_mock: HTTPXMock, *, inserted: int, duplicates: int = 0) -> None:
    httpx_mock.add_response(
        url=f"{API}/api/v1/llm_calls/harvest",
        status_code=201,
        json={"inserted": inserted, "duplicates": duplicates, "byte_offset": 0},
    )


def _harvest_request_body(httpx_mock: HTTPXMock) -> dict:
    requests = [
        r for r in httpx_mock.get_requests() if r.url.path == "/api/v1/llm_calls/harvest"
    ]
    assert len(requests) == 1
    return json.loads(requests[0].content)


# ── Label attribution ────────────────────────────────────────────────────


def test_attribute_label_team_session_dirs() -> None:
    assert attribute_label(TEAM_DIR) == "worker-team1-1"
    assert (
        attribute_label(
            "-home-joe--treadmill-teams-joelepper-treadmill-coordinator-joelepper-treadmill"
        )
        == "coordinator-joelepper-treadmill"
    )
    assert (
        attribute_label("-home-x--treadmill-teams-t-evaluator-t") == "evaluator-t"
    )


def test_attribute_label_non_team_dir_keeps_raw_name() -> None:
    assert attribute_label("-home-joe-treadmill") == "-home-joe-treadmill"
    # Worktree dirs contain "workers-agent" — the role prefix requires a
    # trailing hyphen, so they must NOT be mistaken for worker sessions.
    name = "-home-joe-treadmill--claude-worktrees-agent-a39c-workers-agent"
    assert attribute_label(name) == name


# ── Happy path ───────────────────────────────────────────────────────────


def test_harvest_happy_path(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    """Two calls (one streamed over 3 lines), one matching execution."""
    execution = _execution()
    transcript = _write_transcript(
        tmp_path,
        [
            _user_line(),
            # req_1 streams three lines; the parser must emit ONE call.
            _usage_line("req_1", output_tokens=5),
            _usage_line("req_1", output_tokens=7),
            _usage_line("req_1", output_tokens=9),
            _usage_line("req_2", timestamp="2026-06-10T12:30:00.000Z"),
        ],
    )
    _mock_cursors(httpx_mock, [])
    _mock_executions(httpx_mock, [execution])
    _mock_harvest(httpx_mock, inserted=2)

    result = runner.invoke(
        app, ["tokens", "harvest", "--projects-dir", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    body = _harvest_request_body(httpx_mock)
    assert body["transcript_path"] == str(transcript)
    assert body["byte_offset"] == transcript.stat().st_size
    assert body["malformed_lines_delta"] == 0
    assert len(body["calls"]) == 2
    by_request = {c["request_id"]: c for c in body["calls"]}
    assert set(by_request) == {"req_1", "req_2"}
    # Last streamed line per requestId wins.
    assert by_request["req_1"]["output_tokens"] == 9
    for call in body["calls"]:
        assert call["session_label"] == LABEL
        assert call["task_execution_id"] == execution["id"]
        assert call["model"] == "claude-fable-5"
        assert call["cache_read_tokens"] == 50_000
    assert "2 call(s) inserted" in _plain(result.output)


# ── Idempotent rerun ─────────────────────────────────────────────────────


def test_harvest_idempotent_rerun_posts_nothing(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    """A cursor at EOF means the file is skipped entirely on re-run."""
    transcript = _write_transcript(tmp_path, [_usage_line("req_1")])
    _mock_cursors(
        httpx_mock,
        [
            {
                "transcript_path": str(transcript),
                "byte_offset": transcript.stat().st_size,
                "malformed_lines": 0,
            }
        ],
    )

    result = runner.invoke(
        app, ["tokens", "harvest", "--projects-dir", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    # Only the cursor read happened — no executions fetch, no harvest POST.
    assert len(httpx_mock.get_requests()) == 1
    assert "harvested 0 transcript(s)" in _plain(result.output)


def test_harvest_resumes_from_cursor_offset(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    """Bytes appended after the cursor are harvested; earlier ones are not."""
    transcript = _write_transcript(tmp_path, [_usage_line("req_old")])
    old_size = transcript.stat().st_size
    with transcript.open("a") as fh:
        fh.write(_usage_line("req_new") + "\n")
    _mock_cursors(
        httpx_mock,
        [
            {
                "transcript_path": str(transcript),
                "byte_offset": old_size,
                "malformed_lines": 0,
            }
        ],
    )
    _mock_executions(httpx_mock, [])
    _mock_harvest(httpx_mock, inserted=1)

    result = runner.invoke(
        app, ["tokens", "harvest", "--projects-dir", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    body = _harvest_request_body(httpx_mock)
    assert [c["request_id"] for c in body["calls"]] == ["req_new"]
    assert body["byte_offset"] == transcript.stat().st_size


# ── Malformed lines ──────────────────────────────────────────────────────


def test_harvest_counts_malformed_lines(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    _write_transcript(
        tmp_path,
        [
            "{not json at all",
            _usage_line("req_1"),
            "plain garbage text",
            # Valid JSON but not an object — also malformed.
            "12345",
            # Assistant line with usage but no parsable call fields.
            json.dumps({"type": "assistant", "message": {"usage": {}}}),
        ],
    )
    _mock_cursors(httpx_mock, [])
    _mock_executions(httpx_mock, [])
    _mock_harvest(httpx_mock, inserted=1)

    result = runner.invoke(
        app, ["tokens", "harvest", "--projects-dir", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    body = _harvest_request_body(httpx_mock)
    assert body["malformed_lines_delta"] == 4
    assert len(body["calls"]) == 1
    assert "4 malformed line(s) counted" in _plain(result.output)


def test_parse_span_leaves_partial_trailing_line(tmp_path: Path) -> None:
    """A line still being streamed is not consumed; the cursor stops before it."""
    session_dir = tmp_path / TEAM_DIR
    session_dir.mkdir(parents=True)
    path = session_dir / "s.jsonl"
    complete = _usage_line("req_1") + "\n"
    partial = '{"type": "assistant", "requestId": "req_2", "mes'
    path.write_text(complete + partial)

    span = parse_transcript_span(path, 0)

    assert span.new_offset == len(complete.encode())
    assert list(span.calls) == ["req_1"]
    assert span.malformed == 0


# ── Window-join attribution ──────────────────────────────────────────────


def _at(hour: int) -> datetime:
    return datetime(2026, 6, 10, hour, 0, 0, tzinfo=timezone.utc)


def test_match_execution_exactly_one_window() -> None:
    execution = _execution(
        started_at="2026-06-10T11:00:00+00:00",
        completed_at="2026-06-10T13:00:00+00:00",
    )
    assert match_execution([execution], _at(12)) == execution["id"]


def test_match_execution_open_window_matches() -> None:
    execution = _execution(
        started_at="2026-06-10T11:00:00+00:00", completed_at=None
    )
    assert match_execution([execution], _at(12)) == execution["id"]


def test_match_execution_out_of_window_is_none() -> None:
    execution = _execution(
        started_at="2026-06-10T11:00:00+00:00",
        completed_at="2026-06-10T13:00:00+00:00",
    )
    assert match_execution([execution], _at(14)) is None
    assert match_execution([execution], _at(10)) is None


def test_match_execution_ambiguous_windows_yield_none() -> None:
    overlapping = [
        _execution(
            started_at="2026-06-10T11:00:00+00:00",
            completed_at="2026-06-10T13:00:00+00:00",
        ),
        _execution(
            started_at="2026-06-10T11:30:00+00:00",
            completed_at=None,
        ),
    ]
    assert match_execution(overlapping, _at(12)) is None


def test_harvest_window_join_attribution_end_to_end(
    tmp_path: Path, httpx_mock: HTTPXMock
) -> None:
    """In-window call gets the execution id; out-of-window call gets NULL."""
    execution = _execution(
        started_at="2026-06-10T11:00:00+00:00",
        completed_at="2026-06-10T13:00:00+00:00",
    )
    _write_transcript(
        tmp_path,
        [
            _usage_line("req_in", timestamp="2026-06-10T12:00:00.000Z"),
            _usage_line("req_out", timestamp="2026-06-10T18:00:00.000Z"),
        ],
    )
    _mock_cursors(httpx_mock, [])
    _mock_executions(httpx_mock, [execution])
    _mock_harvest(httpx_mock, inserted=2)

    result = runner.invoke(
        app, ["tokens", "harvest", "--projects-dir", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    by_request = {
        c["request_id"]: c for c in _harvest_request_body(httpx_mock)["calls"]
    }
    assert by_request["req_in"]["task_execution_id"] == execution["id"]
    assert by_request["req_out"]["task_execution_id"] is None


# ── Report ───────────────────────────────────────────────────────────────


def test_report_renders_rollup_and_malformed_total(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=f"{API}/api/v1/llm_calls/report?since=2026-06-10T00%3A00%3A00%2B00%3A00",
        json={
            "since": "2026-06-10T00:00:00+00:00",
            "rows": [
                {
                    "session_label": LABEL,
                    "calls": 1234,
                    "input_tokens": 1_700_000,
                    "output_tokens": 11_000_000,
                    "cache_creation_tokens": 62_300_000,
                    "cache_read_tokens": 7_494_000_000,
                    "cache_hit_ratio": 0.99,
                }
            ],
            "malformed_lines_total": 7,
        },
    )

    result = runner.invoke(app, ["tokens", "report", "--since", "2026-06-10"])

    assert result.exit_code == 0, result.output
    assert LABEL in _plain(result.output)
    assert "1,234" in _plain(result.output)
    assert "99.0%" in _plain(result.output)
    assert "malformed transcript lines counted: 7" in _plain(result.output)


def test_report_rejects_non_iso_since(httpx_mock: HTTPXMock) -> None:
    result = runner.invoke(app, ["tokens", "report", "--since", "yesterday"])
    assert result.exit_code == 2
