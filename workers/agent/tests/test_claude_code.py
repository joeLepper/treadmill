"""Claude Code wrapper tests.

We don't invoke the real ``claude`` CLI in unit tests — instead we
override ``CLAUDE_BINARY`` to a small bash stub and assert the wrapper
shells out with the right flags.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

import pytest

from treadmill_agent import claude_code
from treadmill_agent.api_client import PriorStep, Role


def _role(**overrides) -> Role:
    base = dict(
        id="role-author", model="claude-opus-4-7",
        system_prompt="be a coder",
        output_kind="code",
        skills=[], hooks=[],
    )
    base.update(overrides)
    return Role(**base)


def test_compose_prompt_includes_all_sections() -> None:
    role = _role()
    prompt = claude_code._compose_prompt(
        role=role, task_title="Add a thing",
        task_description="implement X", plan_intent="goal of plan",
    )
    assert "Plan intent" in prompt
    assert "goal of plan" in prompt
    assert "Add a thing" in prompt
    assert "implement X" in prompt
    assert "Instructions" in prompt


def test_compose_prompt_omits_intent_when_none() -> None:
    prompt = claude_code._compose_prompt(
        role=_role(), task_title="t", task_description=None, plan_intent=None,
    )
    assert "Plan intent" not in prompt


def test_compose_prompt_includes_skill_content() -> None:
    from treadmill_agent.api_client import Skill
    role = _role(skills=[
        Skill(id="s1", name="careful", content="be careful"),
        Skill(id="s2", name="terse", content="be terse"),
    ])
    prompt = claude_code._compose_prompt(
        role=role, task_title="t", task_description=None, plan_intent=None,
    )
    assert "Skills available" in prompt
    assert "be careful" in prompt
    assert "be terse" in prompt
    # Skills appear in declared order.
    assert prompt.index("be careful") < prompt.index("be terse")


# ── prior_steps folding (ADR-0015 multi-step workflows) ─────────────────────


def _prior_step(
    *,
    step_index: int = 0,
    step_name: str = "analyzer",
    role_id: str = "role-feedback-analyzer",
    status: str = "completed",
    output: dict | None = None,
) -> PriorStep:
    """Helper — build a ``PriorStep`` with a default analyzer-shaped
    output. Tests override ``output`` to exercise the various payload
    shapes the prompt-composer must handle."""
    return PriorStep(
        step_index=step_index, step_name=step_name,
        role_id=role_id, status=status,
        output=output,
    )


def test_compose_prompt_includes_prior_step_task_directive_when_present() -> None:
    """ADR-0015 §"Inter-step state passing": the action role consumes
    the analyzer's ``task_directive`` via ``prior_steps[-1].output
    .payload.task_directive``. The prompt-composer folds the directive
    in as a structured block under "Prior step output"."""
    prior = _prior_step(output={
        "summary": "Fix typo in foo.py",
        "decision": "plan-ready",
        "payload": {
            "task_directive": {
                "summary": "Correct the spelling of 'recieve' to 'receive'",
                "files": ["foo.py"],
                "intent": "PR review flagged a spelling mistake on line 12",
            },
        },
    })
    prompt = claude_code._compose_prompt(
        role=_role(), task_title="t", task_description=None,
        plan_intent=None, prior_steps=[prior],
    )
    assert "Prior step output" in prompt
    # Summary + intent (from the directive) must surface in the prompt
    # so the action role sees what the analyzer concluded.
    assert "Correct the spelling of 'recieve' to 'receive'" in prompt
    assert "PR review flagged a spelling mistake on line 12" in prompt
    # The structured directive is serialized as JSON so the action
    # role can parse files / scope literally.
    assert "foo.py" in prompt


def test_compose_prompt_omits_prior_steps_section_when_empty() -> None:
    """Single-step workflows (e.g. ``wf-author``) pass ``prior_steps=[]``;
    the "Prior step output" section must not appear."""
    prompt = claude_code._compose_prompt(
        role=_role(), task_title="t", task_description=None,
        plan_intent=None, prior_steps=[],
    )
    assert "Prior step output" not in prompt


def test_compose_prompt_uses_most_recent_prior_step_for_directive() -> None:
    """ADR-0015 §"Q15.c": when multiple prior steps exist, the
    immediately-prior step's directive is the one the action role
    consumes. ``prior_steps`` is ordered ascending by ``step_index``,
    so the most recent is ``[-1]`` — not ``[0]``."""
    older = _prior_step(
        step_index=0, step_name="research",
        role_id="role-planner",
        output={
            "summary": "first prior",
            "decision": "plan-ready",
            "payload": {
                "task_directive": {
                    "summary": "OLD-DIRECTIVE-SUMMARY",
                    "intent": "stale intent text",
                },
            },
        },
    )
    newer = _prior_step(
        step_index=1, step_name="analyzer",
        role_id="role-feedback-analyzer",
        output={
            "summary": "second prior",
            "decision": "plan-ready",
            "payload": {
                "task_directive": {
                    "summary": "NEW-DIRECTIVE-SUMMARY",
                    "intent": "fresh intent text",
                },
            },
        },
    )
    prompt = claude_code._compose_prompt(
        role=_role(), task_title="t", task_description=None,
        plan_intent=None, prior_steps=[older, newer],
    )
    # The newer directive's summary + intent must appear.
    assert "NEW-DIRECTIVE-SUMMARY" in prompt
    assert "fresh intent text" in prompt
    # The older directive's summary + intent must NOT appear — it has
    # been superseded by the immediately-prior step.
    assert "OLD-DIRECTIVE-SUMMARY" not in prompt
    assert "stale intent text" not in prompt


def test_compose_prompt_handles_prior_step_without_task_directive() -> None:
    """Graceful fallback: when the prior step's ``payload`` carries no
    ``task_directive`` (an action-step output, or an analyzer that
    decided ``no-action-needed`` / ``blocked``), the prompt-composer
    still surfaces ``summary`` + ``decision`` so the current role has
    *some* context — rather than dropping the section entirely and
    losing the prior decision."""
    prior = _prior_step(
        step_name="action",
        role_id="role-code-author",
        output={
            "summary": "pushed branch task/abc",
            "decision": "pushed",
            "payload": {"pr_number": 42},  # action output, no task_directive
        },
    )
    prompt = claude_code._compose_prompt(
        role=_role(), task_title="t", task_description=None,
        plan_intent=None, prior_steps=[prior],
    )
    # The section appears.
    assert "Prior step output" in prompt
    # Summary + decision are surfaced as the fallback context.
    assert "pushed branch task/abc" in prompt
    assert "pushed" in prompt
    # No JSON directive block since none was provided.
    assert "Task directive" not in prompt


def test_run_claude_code_passes_model_and_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invoke a stub binary that records its argv to a file. Assert the
    wrapper shells out with the role's model + system_prompt + prompt."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    log = tmp_path / "args.log"

    stub = tmp_path / "fake-claude"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" > "{log}"\n'
        'echo "did the thing"\n'
    )
    stub.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BINARY", str(stub))

    result = claude_code.run_claude_code(
        repo_dir=repo_dir, role=_role(),
        task_title="Add a thing", task_description=None, plan_intent=None,
    )
    assert result.summary == "did the thing"
    args = log.read_text().splitlines()
    assert "--print" in args
    assert "--model" in args
    assert "claude-opus-4-7" in args
    assert "--append-system-prompt" in args
    assert "be a coder" in args
    # ``--permission-mode acceptEdits`` is mandatory in headless mode —
    # without it Claude's Edit / Write tools silently no-op (it emits
    # text describing the change without performing it). Discovered
    # during B.11's real-Claude smoke wiring; the worker would otherwise
    # see "no changes staged" on every real-Claude run.
    assert "--permission-mode" in args
    assert "acceptEdits" in args
    assert args[args.index("--permission-mode") + 1] == "acceptEdits"
    # ADR-0020 token tracking: JSON output mode must be requested so
    # we can parse usage fields from the result envelope.
    assert "--output-format" in args
    assert args[args.index("--output-format") + 1] == "json"
    # ``--print`` must come before ``--model`` in argv so the CLI's
    # headless mode is established before the model flag is parsed.
    # Catches a future arg-order regression where flags get reordered
    # in `_compose_prompt` or `run_claude_code`.
    assert args.index("--print") < args.index("--model")


def test_run_claude_code_raises_on_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    stub = tmp_path / "fake-claude"
    stub.write_text("#!/usr/bin/env bash\necho boom >&2\nexit 7\n")
    stub.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BINARY", str(stub))

    with pytest.raises(claude_code.CodeAuthorError, match="exited 7"):
        claude_code.run_claude_code(
            repo_dir=repo_dir, role=_role(),
            task_title="t", task_description=None, plan_intent=None,
        )


def test_find_binary_fallbacks_to_path_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLAUDE_BINARY", raising=False)
    # PATH lookup should succeed for common binaries; we use 'sh' as a
    # stand-in to confirm shutil.which is consulted.
    monkeypatch.setattr(claude_code.shutil, "which", lambda _: "/usr/bin/sh")
    assert claude_code._find_binary() == "/usr/bin/sh"


def test_find_binary_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_BINARY", raising=False)
    monkeypatch.setattr(claude_code.shutil, "which", lambda _: None)
    with pytest.raises(claude_code.CodeAuthorError, match="not found in PATH"):
        claude_code._find_binary()


# ── ADR-0020 phase 2: stream-and-tag subprocess output ──────────────────────


_LOG_CONTEXT = {
    "task_id": "task-abc",
    "step_id": "step-xyz",
    "run_id": "run-1",
    "plan_id": "plan-1",
    "role": "role-author",
    "model": "claude-opus-4-7",
    "workflow": "wf-author",
}


def _multi_line_stub(stub_path: Path) -> None:
    """Write a small bash stub that emits several stdout lines with a
    short delay between them. The delay forces the streaming path to
    actually read lines as they arrive — if the wrapper still buffers,
    a sufficiently long total runtime would surface here (the suite is
    fast enough that we don't need to assert on timing directly)."""
    stub_path.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'line one'\n"
        "echo 'line two'\n"
        "echo 'line three'\n"
    )
    stub_path.chmod(0o755)


def test_run_claude_code_streams_stdout_lines_to_logger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ADR-0020 phase 2: each line of subprocess stdout is emitted via
    the package logger at INFO with the caller's ``log_context`` fields
    attached. The accumulated stdout is still returned as the summary."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    stub = tmp_path / "fake-claude"
    _multi_line_stub(stub)
    monkeypatch.setenv("CLAUDE_BINARY", str(stub))

    with caplog.at_level(logging.INFO, logger="treadmill.agent.claude_code"):
        result = claude_code.run_claude_code(
            repo_dir=repo_dir, role=_role(),
            task_title="t", task_description=None, plan_intent=None,
            log_context=dict(_LOG_CONTEXT),
        )

    # Joined stdout matches the legacy ``result.stdout.strip()`` contract.
    assert result.summary == "line one\nline two\nline three"

    stdout_records = [
        r for r in caplog.records
        if r.name == "treadmill.agent.claude_code"
        and getattr(r, "stream", None) == "stdout"
    ]
    messages = [r.getMessage() for r in stdout_records]
    assert messages == ["line one", "line two", "line three"]
    for record in stdout_records:
        assert record.levelno == logging.INFO
        # Every structured field the caller passed lands on the record.
        for key, value in _LOG_CONTEXT.items():
            assert getattr(record, key) == value


def test_run_claude_code_streams_stderr_at_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """stderr lines from ``claude`` are tagged ``stream=stderr`` and
    emitted at WARNING so operators can spot them in ``docker logs``."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    stub = tmp_path / "fake-claude"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'progress' >&1\n"
        "echo 'a warning' >&2\n"
        "echo 'another warning' >&2\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BINARY", str(stub))

    with caplog.at_level(logging.DEBUG, logger="treadmill.agent.claude_code"):
        result = claude_code.run_claude_code(
            repo_dir=repo_dir, role=_role(),
            task_title="t", task_description=None, plan_intent=None,
            log_context=dict(_LOG_CONTEXT),
        )

    assert result.summary == "progress"
    stderr_records = [
        r for r in caplog.records
        if r.name == "treadmill.agent.claude_code"
        and getattr(r, "stream", None) == "stderr"
    ]
    messages = [r.getMessage() for r in stderr_records]
    assert messages == ["a warning", "another warning"]
    for record in stderr_records:
        assert record.levelno == logging.WARNING
        assert record.task_id == "task-abc"
        assert record.step_id == "step-xyz"


def test_run_claude_code_nonzero_exit_includes_accumulated_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On non-zero exit the error carries the accumulated stderr so the
    runner's ``step.failed`` event has something diagnostic to surface."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    stub = tmp_path / "fake-claude"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'doing thing'\n"
        "echo 'first error' >&2\n"
        "echo 'second error' >&2\n"
        "exit 3\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BINARY", str(stub))

    with pytest.raises(claude_code.CodeAuthorError) as excinfo:
        claude_code.run_claude_code(
            repo_dir=repo_dir, role=_role(),
            task_title="t", task_description=None, plan_intent=None,
            log_context=dict(_LOG_CONTEXT),
        )
    msg = str(excinfo.value)
    assert "exited 3" in msg
    assert "first error" in msg
    assert "second error" in msg
    assert "doing thing" in msg


def test_run_claude_code_timeout_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``claude`` outlasts ``timeout_seconds`` we kill it and let
    ``subprocess.TimeoutExpired`` propagate — the runner maps this to
    ``step.failed``. The reader threads must finish before re-raise so
    no daemon-thread leak survives the test."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    stub = tmp_path / "fake-claude"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'starting'\n"
        "sleep 10\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BINARY", str(stub))

    with pytest.raises(subprocess.TimeoutExpired):
        claude_code.run_claude_code(
            repo_dir=repo_dir, role=_role(),
            task_title="t", task_description=None, plan_intent=None,
            timeout_seconds=1,
            log_context=dict(_LOG_CONTEXT),
        )


def test_run_claude_code_accepts_no_log_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``log_context`` is optional — the legacy single-arg call still
    works (tests that predate ADR-0020 phase 2 don't have to change)."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    stub = tmp_path / "fake-claude"
    stub.write_text("#!/usr/bin/env bash\necho hi\n")
    stub.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BINARY", str(stub))

    result = claude_code.run_claude_code(
        repo_dir=repo_dir, role=_role(),
        task_title="t", task_description=None, plan_intent=None,
    )
    assert result.summary == "hi"


# ── JSON output parsing (_try_parse_json_output) ─────────────────────────────


def test_try_parse_json_output_extracts_result_and_usage() -> None:
    """Happy path: valid Claude Code JSON envelope yields (result, usage)."""
    payload = {
        "type": "result",
        "subtype": "success",
        "result": "I made the change.",
        "usage": {
            "input_tokens": 120,
            "output_tokens": 30,
            "cache_creation_input_tokens": 5,
            "cache_read_input_tokens": 10,
        },
        "cost_usd": 0.001,
        "duration_ms": 1500,
    }
    import json as _json
    text = _json.dumps(payload)
    summary, usage = claude_code._try_parse_json_output(text)
    assert summary == "I made the change."
    assert usage == {
        "input_tokens": 120,
        "output_tokens": 30,
        "cache_creation_tokens": 5,
        "cache_read_tokens": 10,
    }


def test_try_parse_json_output_falls_back_for_plain_text() -> None:
    """Non-JSON stdout (stub binaries, dry-run) returns (raw, None)."""
    raw = "did the thing\n"
    summary, usage = claude_code._try_parse_json_output(raw)
    assert summary == raw
    assert usage is None


def test_try_parse_json_output_falls_back_for_empty() -> None:
    summary, usage = claude_code._try_parse_json_output("")
    assert usage is None


def test_try_parse_json_output_missing_usage_block() -> None:
    """JSON without a ``usage`` key yields (result, None)."""
    import json as _json
    text = _json.dumps({"type": "result", "result": "ok"})
    summary, usage = claude_code._try_parse_json_output(text)
    assert summary == "ok"
    assert usage is None


def test_try_parse_json_output_zero_cache_tokens() -> None:
    """Zero cache tokens parse to int 0, not missing key."""
    import json as _json
    payload = {
        "result": "done",
        "usage": {
            "input_tokens": 50,
            "output_tokens": 10,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
    _, usage = claude_code._try_parse_json_output(_json.dumps(payload))
    assert usage is not None
    assert usage["cache_creation_tokens"] == 0
    assert usage["cache_read_tokens"] == 0


# ── Token OTel emission via JSON stub ────────────────────────────────────────


def _json_stub(stub_path: Path, result_text: str = "all done") -> None:
    """Write a bash stub that emits the Claude Code JSON envelope."""
    import json as _json
    payload = _json.dumps({
        "type": "result",
        "subtype": "success",
        "result": result_text,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 40,
            "cache_creation_input_tokens": 8,
            "cache_read_input_tokens": 16,
        },
        "cost_usd": 0.002,
        "duration_ms": 2000,
    })
    stub_path.write_text(
        "#!/usr/bin/env bash\n"
        f"echo '{payload}'\n"
    )
    stub_path.chmod(0o755)


def test_run_claude_code_emits_token_metrics_from_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the stub emits a valid JSON envelope, run_claude_code calls
    observability.record_token_usage with the parsed token counts."""
    from unittest.mock import patch, call

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    stub = tmp_path / "fake-claude"
    _json_stub(stub)
    monkeypatch.setenv("CLAUDE_BINARY", str(stub))

    with patch("treadmill_agent.claude_code.observability.record_token_usage") as mock_record:
        result = claude_code.run_claude_code(
            repo_dir=repo_dir, role=_role(),
            task_title="t", task_description=None, plan_intent=None,
            log_context=dict(_LOG_CONTEXT),
        )

    assert result.summary == "all done"
    mock_record.assert_called_once_with(
        model="claude-opus-4-7",
        role="role-author",
        task_id="task-abc",
        step_id="step-xyz",
        input_tokens=100,
        output_tokens=40,
        cache_creation_tokens=8,
        cache_read_tokens=16,
    )


def test_run_claude_code_skips_token_metrics_for_plain_text_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain-text stubs (non-JSON) must not raise; record_token_usage is
    not called and the raw stdout is returned as the summary."""
    from unittest.mock import patch

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    stub = tmp_path / "fake-claude"
    stub.write_text("#!/usr/bin/env bash\necho 'did the thing'\n")
    stub.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BINARY", str(stub))

    with patch("treadmill_agent.claude_code.observability.record_token_usage") as mock_record:
        result = claude_code.run_claude_code(
            repo_dir=repo_dir, role=_role(),
            task_title="t", task_description=None, plan_intent=None,
        )

    assert result.summary == "did the thing"
    mock_record.assert_not_called()


# ── ADR-0020: CodeAuthorResult carries the parsed token_usage + model ───────


def test_run_claude_code_threads_token_usage_into_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0020: when Claude Code emits a JSON envelope with a ``usage``
    block, ``CodeAuthorResult.token_usage`` carries the four parsed
    counters and ``CodeAuthorResult.model`` carries the role's model id.
    The runner reads these to publish ``step.completed.token_usage``."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    stub = tmp_path / "fake-claude"
    _json_stub(stub)
    monkeypatch.setenv("CLAUDE_BINARY", str(stub))

    result = claude_code.run_claude_code(
        repo_dir=repo_dir, role=_role(),
        task_title="t", task_description=None, plan_intent=None,
    )

    assert result.token_usage == {
        "input_tokens": 100,
        "output_tokens": 40,
        "cache_creation_tokens": 8,
        "cache_read_tokens": 16,
    }
    # ``role.model`` is paired with token_usage so the API can attribute
    # the counters to a specific model without round-tripping through ctx.
    assert result.model == "claude-opus-4-7"


def test_run_claude_code_token_usage_none_for_plain_text_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When stdout isn't JSON (stub binaries, dry-run paths, future
    Claude version drift), ``CodeAuthorResult.token_usage`` is ``None``
    and ``model`` is also ``None`` — the runner publishes ``step.completed``
    without a ``token_usage`` field and the API persists NULLs."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    stub = tmp_path / "fake-claude"
    stub.write_text("#!/usr/bin/env bash\necho 'did the thing'\n")
    stub.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BINARY", str(stub))

    result = claude_code.run_claude_code(
        repo_dir=repo_dir, role=_role(),
        task_title="t", task_description=None, plan_intent=None,
    )

    assert result.token_usage is None
    assert result.model is None


# ── ADR-0066: usage-limit fallback ──────────────────────────────────────────


# Captured strings from ``@anthropic-ai/claude-code@2.1.138`` for an
# exhausted OAuth subscription in ``--print --output-format json`` mode.
# The CLI surfaces the message in the JSON envelope's ``result`` field
# (with ``is_error: true``) and / or on stderr; the matcher must catch
# both shapes. These samples are pinned so a future Claude Code bump
# that changes the wording trips the test before it silently breaks the
# fallback path in prod.
_USAGE_LIMIT_JSON_RESULT = (
    '{"type": "result", "is_error": true, "result": '
    '"Claude AI usage limit reached. Your limit resets at 2026-06-04T20:00:00Z."}'
)
_USAGE_LIMIT_STDERR_RATE = (
    "Error: rate_limit_error: This account is rate limited. Please try again later.\n"
)
_USAGE_LIMIT_STDERR_OVERLOADED = "API Error: 429 overloaded_error\n"


def test_looks_like_usage_limit_matches_captured_samples() -> None:
    """Pin the captured-sample strings against the matcher. If Claude
    Code's wording for an exhausted OAuth subscription changes between
    image bumps, this test fires before the fallback path goes silent."""
    assert claude_code.looks_like_usage_limit(_USAGE_LIMIT_JSON_RESULT, "")
    assert claude_code.looks_like_usage_limit("", _USAGE_LIMIT_STDERR_RATE)
    assert claude_code.looks_like_usage_limit("", _USAGE_LIMIT_STDERR_OVERLOADED)
    # Case-insensitive: USAGE LIMIT must match too.
    assert claude_code.looks_like_usage_limit("USAGE LIMIT reached", "")
    # ``quota`` markers, English ``limit reached`` and the standalone
    # ``429`` token all fire on their own.
    assert claude_code.looks_like_usage_limit("monthly quota exhausted", "")
    assert claude_code.looks_like_usage_limit("limit reached for today", "")
    assert claude_code.looks_like_usage_limit("HTTP 429 Too Many Requests", "")


def test_looks_like_usage_limit_ignores_ordinary_failures() -> None:
    """Ordinary, non-quota failure modes must NOT fire the matcher or
    the fallback will silently spend the secondary subscription on
    unrelated errors (transport, auth, invalid args, syntax errors)."""
    assert not claude_code.looks_like_usage_limit("", "Error: connection refused")
    assert not claude_code.looks_like_usage_limit("", "Authentication failed: invalid token")
    assert not claude_code.looks_like_usage_limit("", "Error: unknown flag --frob")
    assert not claude_code.looks_like_usage_limit("syntax error", "")
    assert not claude_code.looks_like_usage_limit("", "")
    # ``4290 ms`` must not look like a 429.
    assert not claude_code.looks_like_usage_limit("", "duration: 4290 ms")


def _creds(account: str = "primary", *, fallback: object = None) -> object:
    """Build a real ``ClaudeCreds`` (used at the ContextVar) so we exercise
    the production code path, not a mock."""
    from treadmill_agent.startup_auth import ClaudeCreds
    return ClaudeCreds(
        account=account, type="oauth", token=f"tkn-{account}",
        fallback=fallback,  # type: ignore[arg-type]
    )


def test_usage_limit_failure_with_fallback_retries_with_fallback_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0066: when the primary's run exits non-zero with a usage-limit
    signature AND the resolved ``ClaudeCreds`` carry a fallback, the
    wrapper re-runs the same cmd once under the fallback's env. The stub
    logs the token env it observed plus the invocation number; the test
    asserts invocation 1 saw the primary token, invocation 2 saw the
    fallback, and that ``record_claude_fallback`` was emitted."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    log = tmp_path / "calls.log"
    stub = tmp_path / "fake-claude"
    # On call 1: usage-limit message + exit 1. On call 2: success JSON
    # envelope (so the wrapper goes down the normal happy path).
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'TOKEN_SEEN="${{CLAUDE_CODE_OAUTH_TOKEN:-NONE}}"\n'
        f'COUNT=$(cat "{log}" 2>/dev/null | wc -l)\n'
        f'echo "call=$((COUNT+1)) token=$TOKEN_SEEN" >> "{log}"\n'
        'if [ "$COUNT" = "0" ]; then\n'
        '  echo "Claude AI usage limit reached. Your limit resets at 2026-06-04T20:00:00Z." >&2\n'
        '  exit 1\n'
        'fi\n'
        '''printf '%s\\n' '{"type":"result","result":"recovered","usage":{"input_tokens":1,"output_tokens":1,"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}'\n'''
    )
    stub.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BINARY", str(stub))

    fallback_creds = _creds("secondary")
    primary = _creds("primary", fallback=fallback_creds)
    token = claude_code.set_claude_creds(primary)
    try:
        from unittest.mock import patch
        with patch(
            "treadmill_agent.claude_code.observability.record_claude_fallback"
        ) as mock_fallback:
            result = claude_code.run_claude_code(
                repo_dir=repo_dir, role=_role(),
                task_title="t", task_description=None, plan_intent=None,
                log_context={"repo": "o/r"},
            )
    finally:
        claude_code.reset_claude_creds(token)

    # The second run's output became the final result.
    assert result.summary == "recovered"

    # Two invocations, primary first, fallback second.
    lines = log.read_text().splitlines()
    assert len(lines) == 2, f"expected 2 invocations, saw {lines!r}"
    assert "call=1 token=tkn-primary" in lines[0]
    assert "call=2 token=tkn-secondary" in lines[1]

    # The OTel counter fired once with the account transition recorded.
    mock_fallback.assert_called_once()
    kwargs = mock_fallback.call_args.kwargs
    assert kwargs["from_account"] == "primary"
    assert kwargs["to_account"] == "secondary"
    assert kwargs["repo"] == "o/r"
    assert kwargs["role"] == "role-author"


def test_usage_limit_failure_without_fallback_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same usage-limit failure, but no fallback configured on the
    resolved creds: the wrapper runs exactly once and raises
    ``CodeAuthorError`` so the runner can map this to ``step.failed``."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    log = tmp_path / "calls.log"
    stub = tmp_path / "fake-claude"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "called" >> "{log}"\n'
        'echo "rate_limit_error: please try again later" >&2\n'
        'exit 1\n'
    )
    stub.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BINARY", str(stub))

    primary = _creds("primary", fallback=None)
    token = claude_code.set_claude_creds(primary)
    try:
        from unittest.mock import patch
        with patch(
            "treadmill_agent.claude_code.observability.record_claude_fallback"
        ) as mock_fallback:
            with pytest.raises(claude_code.CodeAuthorError, match="exited 1"):
                claude_code.run_claude_code(
                    repo_dir=repo_dir, role=_role(),
                    task_title="t", task_description=None, plan_intent=None,
                )
    finally:
        claude_code.reset_claude_creds(token)

    assert log.read_text().splitlines() == ["called"]
    mock_fallback.assert_not_called()


def test_non_usage_limit_failure_with_fallback_does_not_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero exit whose output does NOT match the usage-limit
    matcher must NOT trigger fallback even when one is configured —
    fallback is *only* the usage-limit escape hatch, not a generic
    retry. The runner sees the raw ``CodeAuthorError``."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    log = tmp_path / "calls.log"
    stub = tmp_path / "fake-claude"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "called" >> "{log}"\n'
        'echo "Error: connection refused" >&2\n'
        'exit 2\n'
    )
    stub.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BINARY", str(stub))

    fallback_creds = _creds("secondary")
    primary = _creds("primary", fallback=fallback_creds)
    token = claude_code.set_claude_creds(primary)
    try:
        from unittest.mock import patch
        with patch(
            "treadmill_agent.claude_code.observability.record_claude_fallback"
        ) as mock_fallback:
            with pytest.raises(claude_code.CodeAuthorError, match="exited 2"):
                claude_code.run_claude_code(
                    repo_dir=repo_dir, role=_role(),
                    task_title="t", task_description=None, plan_intent=None,
                )
    finally:
        claude_code.reset_claude_creds(token)

    assert log.read_text().splitlines() == ["called"]
    mock_fallback.assert_not_called()


def test_run_claude_usage_limit_retries_with_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lightweight ``run_claude`` wrapper shares the fallback retry
    with ``run_claude_code`` (both routed through ``_run_claude_with_fallback``)."""
    log = tmp_path / "calls.log"
    stub = tmp_path / "fake-claude"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'TOKEN_SEEN="${{CLAUDE_CODE_OAUTH_TOKEN:-NONE}}"\n'
        f'COUNT=$(cat "{log}" 2>/dev/null | wc -l)\n'
        f'echo "call=$((COUNT+1)) token=$TOKEN_SEEN" >> "{log}"\n'
        'if [ "$COUNT" = "0" ]; then\n'
        '  echo "usage limit reached" >&2\n'
        '  exit 1\n'
        'fi\n'
        'echo "ok"\n'
    )
    stub.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BINARY", str(stub))

    fallback_creds = _creds("secondary")
    primary = _creds("primary", fallback=fallback_creds)
    token = claude_code.set_claude_creds(primary)
    try:
        from unittest.mock import patch
        with patch(
            "treadmill_agent.claude_code.observability.record_claude_fallback"
        ) as mock_fallback:
            out = claude_code.run_claude(
                prompt="x", model="claude-haiku-4-5-20251001",
            )
    finally:
        claude_code.reset_claude_creds(token)

    assert out.strip() == "ok"
    lines = log.read_text().splitlines()
    assert len(lines) == 2
    assert "token=tkn-primary" in lines[0]
    assert "token=tkn-secondary" in lines[1]
    mock_fallback.assert_called_once()
