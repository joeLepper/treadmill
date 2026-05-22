"""Tests for validation_runtime module (ADR-0029).

Exercises both deterministic (subprocess) and LLM-judge (Claude)
check execution paths, including error cases.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest

from treadmill_agent.validation_runtime import (
    CheckResult,
    ValidationVerdict,
    gather_agent_md_context,
    run_deterministic,
    run_llm_judge,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _check(**kwargs: Any) -> Any:
    """Synthetic check object for testing."""
    defaults = {
        "id": "check-123",
        "kind": "deterministic",
        "severity": "blocking",
        "script": "echo test",
        "prompt": "Is this good?",
    }
    defaults.update(kwargs)
    return MagicMock(**defaults)


# ── Deterministic tests ──────────────────────────────────────────────────────


def test_deterministic_pass(tmp_path: Path) -> None:
    """Exit 0 → verdict=pass."""
    check = _check(script="exit 0")
    result = run_deterministic(check, tmp_path, timeout_seconds=5)

    assert result.check_id == "check-123"
    assert result.kind == "deterministic"
    assert result.severity == "blocking"
    assert result.verdict == "pass"
    assert "exit 0" in result.rationale


def test_deterministic_fail(tmp_path: Path) -> None:
    """Non-zero exit → verdict=fail."""
    check = _check(script="exit 42")
    result = run_deterministic(check, tmp_path, timeout_seconds=5)

    assert result.verdict == "fail"
    assert "42" in result.rationale
    assert result.kind == "deterministic"


def test_deterministic_timeout(tmp_path: Path) -> None:
    """TimeoutExpired → verdict=error with timeout message."""
    check = _check(script="sleep 100")
    result = run_deterministic(check, tmp_path, timeout_seconds=1)

    assert result.verdict == "error"
    assert "timeout" in result.rationale.lower()
    assert "1" in result.rationale  # timeout duration


def test_deterministic_exception(tmp_path: Path) -> None:
    """Command not found → verdict=fail (non-zero exit)."""
    check = _check(script="nonexistent-command-xyz 123")
    result = run_deterministic(check, tmp_path, timeout_seconds=5)

    # Command not found will fail with non-zero exit, not raise exception
    assert result.verdict == "fail"


def test_deterministic_stderr_captured(tmp_path: Path) -> None:
    """Stderr output is captured in log_excerpt."""
    check = _check(script='echo "error message" >&2; exit 1')
    result = run_deterministic(check, tmp_path, timeout_seconds=5)

    assert result.verdict == "fail"
    assert "error message" in result.log_excerpt


def test_deterministic_stdout_captured(tmp_path: Path) -> None:
    """Stdout output is also captured in log_excerpt — pytest writes its
    failure summary to stdout, not stderr."""
    check = _check(script='echo "pytest FAILED in test_x"; exit 1')
    result = run_deterministic(check, tmp_path, timeout_seconds=5)

    assert result.verdict == "fail"
    assert "pytest FAILED in test_x" in result.log_excerpt


def test_deterministic_large_output_truncated(tmp_path: Path) -> None:
    """Very large output is truncated to the last ~4000 chars."""
    large_output = "x" * 6000
    check = _check(script=f'echo "{large_output}" >&2; exit 1')
    result = run_deterministic(check, tmp_path, timeout_seconds=5)

    assert result.verdict == "fail"
    assert len(result.log_excerpt) <= 4000


# ── LLM-judge tests ──────────────────────────────────────────────────────────


def test_llm_judge_pass(tmp_path: Path) -> None:
    """Happy path: parse pass verdict from JSON envelope."""
    check = _check(kind="llm-judge", prompt="Is this good?")
    diff = "added: useful feature"
    task_spec = "Add a feature"
    model = "claude-haiku-4-5-20251001"

    # Dedent so the closing JSON fence is at column 0 — the regex
    # in validation_runtime._parse_validation_envelope (mirroring
    # ADR-0027's review path) expects an unindented closing fence.
    # Real Claude output produces column-0 fences; Python triple-
    # quoted strings need dedent to match.
    llm_output = textwrap.dedent("""
        The code looks good.

        ```json
        {
            "verdict": "pass",
            "rationale": "The implementation is correct and well-tested."
        }
        ```
        """)

    with patch("treadmill_agent.claude_code.run_claude") as mock_run:
        mock_run.return_value = llm_output
        result = run_llm_judge(
            check, tmp_path, diff, task_spec, model, timeout_seconds=5
        )

    assert result.verdict == "pass"
    assert result.kind == "llm-judge"
    assert "correct and well-tested" in result.rationale


def test_llm_judge_fail(tmp_path: Path) -> None:
    """Happy path: parse fail verdict from JSON envelope."""
    check = _check(kind="llm-judge", prompt="Does this work?")
    diff = "removed: important check"
    task_spec = "Fix the bug"
    model = "claude-haiku-4-5-20251001"

    llm_output = textwrap.dedent("""
        The logic looks broken.

        ```json
        {
            "verdict": "fail",
            "rationale": "The implementation removes a critical safety check."
        }
        ```
        """)

    with patch("treadmill_agent.claude_code.run_claude") as mock_run:
        mock_run.return_value = llm_output
        result = run_llm_judge(
            check, tmp_path, diff, task_spec, model, timeout_seconds=5
        )

    assert result.verdict == "fail"
    assert "critical safety check" in result.rationale


def test_llm_judge_parse_failure_no_fence(tmp_path: Path) -> None:
    """No JSON fence → verdict=error."""
    check = _check(kind="llm-judge")
    llm_output = "The code looks okay. No JSON here."

    with patch("treadmill_agent.claude_code.run_claude") as mock_run:
        mock_run.return_value = llm_output
        result = run_llm_judge(
            check, tmp_path, "diff", "spec", "model", timeout_seconds=5
        )

    assert result.verdict == "error"
    assert "JSON fence" in result.rationale or "No JSON" in result.rationale


def test_llm_judge_parse_failure_invalid_json(tmp_path: Path) -> None:
    """Invalid JSON in fence → verdict=error."""
    check = _check(kind="llm-judge")
    llm_output = textwrap.dedent("""
        ```json
        { "verdict": "pass", "rationale": invalid json }
        ```
        """)

    with patch("treadmill_agent.claude_code.run_claude") as mock_run:
        mock_run.return_value = llm_output
        result = run_llm_judge(
            check, tmp_path, "diff", "spec", "model", timeout_seconds=5
        )

    assert result.verdict == "error"
    assert "parse" in result.rationale.lower()


def test_llm_judge_parse_failure_missing_field(tmp_path: Path) -> None:
    """Missing required field in JSON → verdict=error."""
    check = _check(kind="llm-judge")
    llm_output = textwrap.dedent("""
        ```json
        {
            "verdict": "pass"
        }
        ```
        """)

    with patch("treadmill_agent.claude_code.run_claude") as mock_run:
        mock_run.return_value = llm_output
        result = run_llm_judge(
            check, tmp_path, "diff", "spec", "model", timeout_seconds=5
        )

    assert result.verdict == "error"
    assert "parse" in result.rationale.lower()


def test_llm_judge_claude_exception(tmp_path: Path) -> None:
    """Exception from run_claude → verdict=error."""
    check = _check(kind="llm-judge")

    with patch("treadmill_agent.claude_code.run_claude") as mock_run:
        mock_run.side_effect = Exception("Claude binary not found")
        result = run_llm_judge(
            check, tmp_path, "diff", "spec", "model", timeout_seconds=5
        )

    assert result.verdict == "error"
    assert "Exception" in result.rationale
    assert "Claude binary not found" in result.rationale


def test_llm_judge_prompt_composition(tmp_path: Path) -> None:
    """Verify prompt is composed correctly from parts."""
    check = _check(kind="llm-judge", prompt="Custom criterion here")
    diff = "some diff content"
    task_spec = "some task spec"
    model = "model-x"

    with patch("treadmill_agent.claude_code.run_claude") as mock_run:
        mock_run.return_value = '```json\n{"verdict":"pass","rationale":"ok"}\n```'
        run_llm_judge(check, tmp_path, diff, task_spec, model, timeout_seconds=5)

    # Verify the prompt was composed with all parts
    call_args = mock_run.call_args
    assert call_args is not None
    prompt = call_args.kwargs["prompt"]
    assert "Custom criterion here" in prompt
    assert "some diff content" in prompt
    assert "some task spec" in prompt
    assert "## PR diff" in prompt
    assert "## Task spec" in prompt


def test_llm_judge_uses_provided_model(tmp_path: Path) -> None:
    """Verify the provided model is passed to run_claude."""
    check = _check(kind="llm-judge")
    model = "custom-model-v1"

    with patch("treadmill_agent.claude_code.run_claude") as mock_run:
        mock_run.return_value = '```json\n{"verdict":"pass","rationale":"ok"}\n```'
        run_llm_judge(check, tmp_path, "diff", "spec", model, timeout_seconds=5)

    call_args = mock_run.call_args
    assert call_args is not None
    assert call_args.kwargs["model"] == model


# ── gather_agent_md_context tests ────────────────────────────────────────────


def test_gather_agent_md_context_returns_nearest(tmp_path: Path) -> None:
    """Diff touching a file under services/api returns that component's
    AGENT.md content, prefixed with a ### relpath header."""
    component = tmp_path / "services" / "api"
    component.mkdir(parents=True)
    agent_md = component / "AGENT.md"
    agent_md.write_text("# services/api\n\nKey surfaces: foo.py\n")

    diff = (
        "diff --git a/services/api/foo.py b/services/api/foo.py\n"
        "--- a/services/api/foo.py\n"
        "+++ b/services/api/foo.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    result = gather_agent_md_context(tmp_path, diff)

    assert "### services/api/AGENT.md" in result
    assert "Key surfaces: foo.py" in result


def test_gather_agent_md_context_no_ancestor_returns_empty(tmp_path: Path) -> None:
    """Diff touching a path with no ancestor AGENT.md returns ''."""
    (tmp_path / "lonely").mkdir()
    diff = (
        "diff --git a/lonely/bar.py b/lonely/bar.py\n"
        "--- a/lonely/bar.py\n"
        "+++ b/lonely/bar.py\n"
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
    )

    assert gather_agent_md_context(tmp_path, diff) == ""


def test_gather_agent_md_context_dedupes(tmp_path: Path) -> None:
    """Two touched files sharing one AGENT.md ancestor produce one block."""
    component = tmp_path / "services" / "api"
    component.mkdir(parents=True)
    (component / "AGENT.md").write_text("# services/api\nDOC_CONTENT\n")

    diff = (
        "diff --git a/services/api/foo.py b/services/api/foo.py\n"
        "--- a/services/api/foo.py\n"
        "+++ b/services/api/foo.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/services/api/bar.py b/services/api/bar.py\n"
        "--- a/services/api/bar.py\n"
        "+++ b/services/api/bar.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    result = gather_agent_md_context(tmp_path, diff)

    assert result.count("### services/api/AGENT.md") == 1
    assert result.count("DOC_CONTENT") == 1


def test_gather_agent_md_context_walks_up_to_repo_root(tmp_path: Path) -> None:
    """When no nearer AGENT.md exists, the walk finds an ancestor's."""
    (tmp_path / "AGENT.md").write_text("# repo\nROOT_DOCS\n")
    nested = tmp_path / "services" / "api"
    nested.mkdir(parents=True)

    diff = (
        "--- a/services/api/foo.py\n"
        "+++ b/services/api/foo.py\n"
    )

    result = gather_agent_md_context(tmp_path, diff)

    assert "### AGENT.md" in result
    assert "ROOT_DOCS" in result


def test_gather_agent_md_context_empty_diff(tmp_path: Path) -> None:
    """Empty or None diff returns ''."""
    assert gather_agent_md_context(tmp_path, "") == ""
    assert gather_agent_md_context(tmp_path, None) == ""  # type: ignore[arg-type]


def test_gather_agent_md_context_skips_dev_null(tmp_path: Path) -> None:
    """+++ /dev/null lines (file deletions) are ignored."""
    diff = "--- a/foo.py\n+++ /dev/null\n"
    assert gather_agent_md_context(tmp_path, diff) == ""


def test_run_llm_judge_includes_agent_md(tmp_path: Path) -> None:
    """run_llm_judge injects component AGENT.md content into the prompt
    under an ## AGENT_MD section placed before ## PR diff."""
    component = tmp_path / "services" / "api"
    component.mkdir(parents=True)
    (component / "AGENT.md").write_text("# services/api\nINJECTED_DOC_CONTENT\n")

    check = _check(kind="llm-judge", prompt="Are the docs current?")
    diff = (
        "diff --git a/services/api/foo.py b/services/api/foo.py\n"
        "--- a/services/api/foo.py\n"
        "+++ b/services/api/foo.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    with patch("treadmill_agent.claude_code.run_claude") as mock_run:
        mock_run.return_value = '```json\n{"verdict":"pass","rationale":"ok"}\n```'
        run_llm_judge(check, tmp_path, diff, "spec", "model", timeout_seconds=5)

    prompt = mock_run.call_args.kwargs["prompt"]
    assert "## AGENT_MD" in prompt
    assert "INJECTED_DOC_CONTENT" in prompt
    assert "### services/api/AGENT.md" in prompt
    # AGENT_MD section sits before the PR diff section.
    assert prompt.index("## AGENT_MD") < prompt.index("## PR diff")


def test_run_llm_judge_no_agent_md_section_when_absent(tmp_path: Path) -> None:
    """When no AGENT.md governs the touched paths, the prompt omits the
    ## AGENT_MD section (avoids confusing the judge with an empty block)."""
    check = _check(kind="llm-judge", prompt="criterion")
    diff = "--- a/orphan.py\n+++ b/orphan.py\n"

    with patch("treadmill_agent.claude_code.run_claude") as mock_run:
        mock_run.return_value = '```json\n{"verdict":"pass","rationale":"ok"}\n```'
        run_llm_judge(check, tmp_path, diff, "spec", "model", timeout_seconds=5)

    prompt = mock_run.call_args.kwargs["prompt"]
    assert "## AGENT_MD" not in prompt


# ── ValidationVerdict model tests ────────────────────────────────────────────


def test_validation_verdict_valid_pass() -> None:
    """Valid pass verdict parses successfully."""
    verdict = ValidationVerdict(
        verdict="pass",
        rationale="The implementation is correct.",
    )
    assert verdict.verdict == "pass"
    assert verdict.rationale == "The implementation is correct."


def test_validation_verdict_valid_fail() -> None:
    """Valid fail verdict parses successfully."""
    verdict = ValidationVerdict(
        verdict="fail",
        rationale="The implementation is broken.",
    )
    assert verdict.verdict == "fail"


def test_validation_verdict_invalid_verdict_value() -> None:
    """Invalid verdict value is rejected."""
    with pytest.raises(Exception):  # ValidationError
        ValidationVerdict(
            verdict="maybe",  # type: ignore
            rationale="Invalid verdict",
        )


def test_validation_verdict_rationale_required() -> None:
    """Missing rationale is rejected."""
    with pytest.raises(Exception):  # ValidationError
        ValidationVerdict(verdict="pass")  # type: ignore


def test_validation_verdict_extra_fields_forbidden() -> None:
    """Extra fields are rejected (strict mode)."""
    with pytest.raises(Exception):  # ValidationError
        ValidationVerdict(
            verdict="pass",
            rationale="ok",
            extra_field="not allowed",  # type: ignore
        )


def test_validation_verdict_rationale_max_length() -> None:
    """Rationale respects max_length constraint."""
    long_rationale = "x" * 5000
    with pytest.raises(Exception):  # ValidationError
        ValidationVerdict(verdict="pass", rationale=long_rationale)


# ── CheckResult dataclass tests ──────────────────────────────────────────────


def test_check_result_frozen() -> None:
    """CheckResult is immutable (frozen dataclass)."""
    result = CheckResult(
        check_id="id",
        kind="deterministic",
        severity="blocking",
        verdict="pass",
        rationale="ok",
        log_excerpt="",
    )
    with pytest.raises(Exception):  # FrozenInstanceError
        result.verdict = "fail"  # type: ignore


def test_check_result_structure() -> None:
    """CheckResult has all expected fields."""
    result = CheckResult(
        check_id="check-xyz",
        kind="llm-judge",
        severity="advisory",
        verdict="fail",
        rationale="The feature is not well-documented.",
        log_excerpt="Some output",
    )
    assert result.check_id == "check-xyz"
    assert result.kind == "llm-judge"
    assert result.severity == "advisory"
    assert result.verdict == "fail"
    assert result.rationale == "The feature is not well-documented."
    assert result.log_excerpt == "Some output"
