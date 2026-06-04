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
    gather_adjacent_docs_context,
    gather_agent_md_context,
    gather_cited_adrs_context,
    gather_cited_plans_context,
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


def test_deterministic_subprocess_env_excludes_install_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0060 task-phase contract: validation subprocess env carries
    only the uncredentialed HTTPS_PROXY (from the worker entrypoint),
    NEVER the credentialed install proxy URL. If this leaked, a task-
    phase validation script could reach the install-phase allowlist
    via the egress proxy — the entire phase-toggle correctness depends
    on the credential being absent here.

    The credential lives only in materialize()'s subprocess env
    (covered by tests in test_repo_deps.py); this test pins the
    *absence* on the validation seam."""
    monkeypatch.setenv("TREADMILL_INSTALL_PROXY_TOKEN", "abc123")
    monkeypatch.setenv("HTTPS_PROXY", "http://treadmill-egress-proxy:3128")

    check = _check(script="exit 0")
    with patch(
        "treadmill_agent.validation_runtime.subprocess.run"
    ) as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        run_deterministic(check, tmp_path, timeout_seconds=5)

    env = mock_run.call_args.kwargs["env"]
    # The base (uncredentialed) HTTPS_PROXY may pass through unchanged
    # — that's the task-phase default. But no credential userinfo.
    https_proxy = env.get("HTTPS_PROXY", "")
    assert "install:" not in https_proxy
    assert "abc123" not in https_proxy
    assert https_proxy == "http://treadmill-egress-proxy:3128"


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


# ── gather_cited_adrs_context tests ──────────────────────────────────────────


def test_gather_cited_adrs_context_resolves_referenced_adr(tmp_path: Path) -> None:
    """A diff that names ``ADR-0123`` returns that ADR's body under a
    ``### docs/adrs/...md`` header."""
    adrs_dir = tmp_path / "docs" / "adrs"
    adrs_dir.mkdir(parents=True)
    (adrs_dir / "0123-something-important.md").write_text(
        "# ADR-0123\n\nADR_BODY_CONTENT\n"
    )

    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+see ADR-0123 for context\n"
    )

    result = gather_cited_adrs_context(diff, tmp_path)

    assert "### docs/adrs/0123-something-important.md" in result
    assert "ADR_BODY_CONTENT" in result


def test_gather_cited_adrs_context_empty_when_no_refs(tmp_path: Path) -> None:
    """Diff with no ADR-NNNN tokens returns ''."""
    (tmp_path / "docs" / "adrs").mkdir(parents=True)
    diff = "--- a/foo.py\n+++ b/foo.py\n+ no adr here\n"
    assert gather_cited_adrs_context(diff, tmp_path) == ""


def test_gather_cited_adrs_context_skips_missing_adr(tmp_path: Path) -> None:
    """A reference to a non-existent ADR is silently skipped."""
    (tmp_path / "docs" / "adrs").mkdir(parents=True)
    diff = "--- a/foo.py\n+++ b/foo.py\n+see ADR-9999\n"
    assert gather_cited_adrs_context(diff, tmp_path) == ""


def test_gather_cited_adrs_context_dedupes_repeated_refs(tmp_path: Path) -> None:
    """Repeated ADR-NNNN mentions yield one block."""
    adrs_dir = tmp_path / "docs" / "adrs"
    adrs_dir.mkdir(parents=True)
    (adrs_dir / "0042-dup.md").write_text("ONE_BLOCK_ONLY\n")
    diff = "--- a/foo.py\n+++ b/foo.py\n+ADR-0042 and again ADR-0042\n"
    result = gather_cited_adrs_context(diff, tmp_path)
    assert result.count("ONE_BLOCK_ONLY") == 1


def test_gather_cited_adrs_context_empty_diff(tmp_path: Path) -> None:
    assert gather_cited_adrs_context("", tmp_path) == ""


# ── gather_cited_plans_context tests ─────────────────────────────────────────


def test_gather_cited_plans_context_resolves_referenced_plan(
    tmp_path: Path,
) -> None:
    """A diff that names ``docs/plans/2026-05-21-foo.md`` returns that
    plan's body under a header."""
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "2026-05-21-foo.md").write_text("PLAN_BODY_CONTENT\n")

    diff = (
        "--- a/bar.py\n"
        "+++ b/bar.py\n"
        "+# implements docs/plans/2026-05-21-foo.md\n"
    )

    result = gather_cited_plans_context(diff, tmp_path)

    assert "### docs/plans/2026-05-21-foo.md" in result
    assert "PLAN_BODY_CONTENT" in result


def test_gather_cited_plans_context_skips_missing_plan(tmp_path: Path) -> None:
    """Reference to a non-existent plan path returns ''."""
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    diff = "+see docs/plans/2026-05-21-not-real.md\n"
    assert gather_cited_plans_context(diff, tmp_path) == ""


def test_gather_cited_plans_context_empty_when_no_refs(tmp_path: Path) -> None:
    diff = "--- a/foo.py\n+++ b/foo.py\n+no plan here\n"
    assert gather_cited_plans_context(diff, tmp_path) == ""


def test_gather_cited_plans_context_empty_diff(tmp_path: Path) -> None:
    assert gather_cited_plans_context("", tmp_path) == ""


# ── gather_adjacent_docs_context tests ───────────────────────────────────────


def test_gather_adjacent_docs_context_returns_sibling_markdown(
    tmp_path: Path,
) -> None:
    """A touched file with a README.md sibling returns the README content."""
    component = tmp_path / "services" / "api"
    component.mkdir(parents=True)
    (component / "README.md").write_text("ADJACENT_README_CONTENT\n")

    diff = (
        "--- a/services/api/foo.py\n"
        "+++ b/services/api/foo.py\n"
    )

    result = gather_adjacent_docs_context(diff, tmp_path)

    assert "### services/api/README.md" in result
    assert "ADJACENT_README_CONTENT" in result


def test_gather_adjacent_docs_context_skips_agent_md(tmp_path: Path) -> None:
    """``AGENT.md`` is supplied by ``gather_agent_md_context`` and must
    not also appear in the adjacent-docs block."""
    component = tmp_path / "services" / "api"
    component.mkdir(parents=True)
    (component / "AGENT.md").write_text("AGENT_MD_BODY\n")
    (component / "README.md").write_text("README_BODY\n")

    diff = "--- a/services/api/foo.py\n+++ b/services/api/foo.py\n"

    result = gather_adjacent_docs_context(diff, tmp_path)

    assert "AGENT_MD_BODY" not in result
    assert "README_BODY" in result


def test_gather_adjacent_docs_context_finds_docs_sibling(tmp_path: Path) -> None:
    """A docs/ directory next to the touched file's parent is harvested."""
    (tmp_path / "services" / "api").mkdir(parents=True)
    docs = tmp_path / "services" / "docs"
    docs.mkdir(parents=True)
    (docs / "design.md").write_text("DESIGN_NOTES\n")

    diff = "--- a/services/api/foo.py\n+++ b/services/api/foo.py\n"

    result = gather_adjacent_docs_context(diff, tmp_path)

    assert "### services/docs/design.md" in result
    assert "DESIGN_NOTES" in result


def test_gather_adjacent_docs_context_empty_when_none(tmp_path: Path) -> None:
    (tmp_path / "lonely").mkdir()
    diff = "--- a/lonely/bar.py\n+++ b/lonely/bar.py\n"
    assert gather_adjacent_docs_context(diff, tmp_path) == ""


def test_gather_adjacent_docs_context_truncates_above_cap(tmp_path: Path) -> None:
    """Total content above the ~50k char cap is truncated, with a
    sentinel appended so the judge knows the input was clipped."""
    component = tmp_path / "services" / "api"
    component.mkdir(parents=True)
    big = "X" * 60_000
    (component / "big.md").write_text(big)

    diff = "--- a/services/api/foo.py\n+++ b/services/api/foo.py\n"

    result = gather_adjacent_docs_context(diff, tmp_path)

    # The cap is ~50k; allow up to ~50.5k including the sentinel.
    assert len(result) < 51_000
    assert "truncated" in result.lower()


def test_gather_adjacent_docs_context_empty_diff(tmp_path: Path) -> None:
    assert gather_adjacent_docs_context("", tmp_path) == ""


# ── run_llm_judge integration with all three new sections ────────────────────


def test_run_llm_judge_includes_cited_adrs(tmp_path: Path) -> None:
    """ADR cited in the diff appears in a ## CITED_ADRS section."""
    adrs_dir = tmp_path / "docs" / "adrs"
    adrs_dir.mkdir(parents=True)
    (adrs_dir / "0077-cited.md").write_text("CITED_ADR_BODY\n")

    check = _check(kind="llm-judge", prompt="criterion")
    diff = (
        "--- a/foo.py\n+++ b/foo.py\n"
        "+# rationale per ADR-0077\n"
    )

    with patch("treadmill_agent.claude_code.run_claude") as mock_run:
        mock_run.return_value = '```json\n{"verdict":"pass","rationale":"ok"}\n```'
        run_llm_judge(check, tmp_path, diff, "spec", "model", timeout_seconds=5)

    prompt = mock_run.call_args.kwargs["prompt"]
    assert "## CITED_ADRS" in prompt
    assert "CITED_ADR_BODY" in prompt


def test_run_llm_judge_includes_cited_plans(tmp_path: Path) -> None:
    """Plan cited by path in the diff appears in a ## CITED_PLANS section."""
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "2026-05-21-x.md").write_text("CITED_PLAN_BODY\n")

    check = _check(kind="llm-judge", prompt="criterion")
    diff = (
        "--- a/foo.py\n+++ b/foo.py\n"
        "+# implements docs/plans/2026-05-21-x.md\n"
    )

    with patch("treadmill_agent.claude_code.run_claude") as mock_run:
        mock_run.return_value = '```json\n{"verdict":"pass","rationale":"ok"}\n```'
        run_llm_judge(check, tmp_path, diff, "spec", "model", timeout_seconds=5)

    prompt = mock_run.call_args.kwargs["prompt"]
    assert "## CITED_PLANS" in prompt
    assert "CITED_PLAN_BODY" in prompt


def test_run_llm_judge_includes_adjacent_docs(tmp_path: Path) -> None:
    """Adjacent ``*.md`` sibling appears under a ## ADJACENT_DOCS section."""
    component = tmp_path / "services" / "api"
    component.mkdir(parents=True)
    (component / "README.md").write_text("ADJACENT_README_BODY\n")

    check = _check(kind="llm-judge", prompt="criterion")
    diff = (
        "--- a/services/api/foo.py\n"
        "+++ b/services/api/foo.py\n"
    )

    with patch("treadmill_agent.claude_code.run_claude") as mock_run:
        mock_run.return_value = '```json\n{"verdict":"pass","rationale":"ok"}\n```'
        run_llm_judge(check, tmp_path, diff, "spec", "model", timeout_seconds=5)

    prompt = mock_run.call_args.kwargs["prompt"]
    assert "## ADJACENT_DOCS" in prompt
    assert "ADJACENT_README_BODY" in prompt


def test_run_llm_judge_section_order_when_all_present(tmp_path: Path) -> None:
    """When all four context sources exist, the prompt assembles them in
    AGENT_MD → CITED_ADRS → CITED_PLANS → ADJACENT_DOCS → PR diff →
    Task spec order. This is what callers (judge prompts) declare they
    expect."""
    component = tmp_path / "services" / "api"
    component.mkdir(parents=True)
    (component / "AGENT.md").write_text("AGENT_MD_BODY\n")
    (component / "README.md").write_text("ADJACENT_README_BODY\n")
    adrs_dir = tmp_path / "docs" / "adrs"
    adrs_dir.mkdir(parents=True)
    (adrs_dir / "0077-cited.md").write_text("CITED_ADR_BODY\n")
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "2026-05-21-x.md").write_text("CITED_PLAN_BODY\n")

    check = _check(kind="llm-judge", prompt="criterion")
    diff = (
        "--- a/services/api/foo.py\n"
        "+++ b/services/api/foo.py\n"
        "+# per ADR-0077 and docs/plans/2026-05-21-x.md\n"
    )

    with patch("treadmill_agent.claude_code.run_claude") as mock_run:
        mock_run.return_value = '```json\n{"verdict":"pass","rationale":"ok"}\n```'
        run_llm_judge(check, tmp_path, diff, "spec", "model", timeout_seconds=5)

    prompt = mock_run.call_args.kwargs["prompt"]
    sections = [
        "## AGENT_MD",
        "## CITED_ADRS",
        "## CITED_PLANS",
        "## ADJACENT_DOCS",
        "## PR diff",
        "## Task spec",
    ]
    positions = [prompt.index(s) for s in sections]
    assert positions == sorted(positions)


def test_run_llm_judge_omits_sections_when_empty(tmp_path: Path) -> None:
    """When the diff cites nothing and no adjacent docs exist, none of
    the three new section headers appear (an empty ``## CITED_ADRS``
    block would mislead the judge — the docs-currency false-pass we are
    trying to avoid)."""
    check = _check(kind="llm-judge", prompt="criterion")
    diff = "--- a/orphan.py\n+++ b/orphan.py\n"

    with patch("treadmill_agent.claude_code.run_claude") as mock_run:
        mock_run.return_value = '```json\n{"verdict":"pass","rationale":"ok"}\n```'
        run_llm_judge(check, tmp_path, diff, "spec", "model", timeout_seconds=5)

    prompt = mock_run.call_args.kwargs["prompt"]
    assert "## CITED_ADRS" not in prompt
    assert "## CITED_PLANS" not in prompt
    assert "## ADJACENT_DOCS" not in prompt


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


def test_validation_verdict_accepts_result_alias() -> None:
    """``{"result": ...}`` is accepted as an alias for ``{"verdict": ...}``.

    Motivation: 116 judge runs on the 2026-05-21 validator-corpus triage
    errored on a strict-envelope ``ValidationError`` because the model
    emitted ``result`` instead of ``verdict``. Widening the envelope
    here recovers those verdicts.
    """
    verdict = ValidationVerdict.model_validate(
        {"result": "pass", "rationale": "x"}
    )
    assert verdict.verdict == "pass"


def test_validation_verdict_accepts_result_alias_fail() -> None:
    """``{"result": "fail", ...}`` also parses cleanly."""
    verdict = ValidationVerdict.model_validate(
        {"result": "fail", "rationale": "x"}
    )
    assert verdict.verdict == "fail"


def test_validation_verdict_field_name_still_works() -> None:
    """``{"verdict": ...}`` (the original field name) keeps parsing —
    ``populate_by_name=True`` accepts EITHER input shape so existing
    judges that emit ``verdict`` are untouched."""
    verdict = ValidationVerdict.model_validate(
        {"verdict": "pass", "rationale": "x"}
    )
    assert verdict.verdict == "pass"


def test_validation_verdict_both_keys_present_conflict() -> None:
    """Behavior pin: both ``verdict`` and ``result`` present with
    conflicting values resolves deterministically.

    With ``Field(alias="result")`` + ``populate_by_name=True`` +
    ``extra="forbid"``, Pydantic v2 consumes the alias first and treats
    the leftover field-name key as extra, raising ``ValidationError``.
    Calling the validator twice on the same input must produce the
    same outcome — pinning that here so a future Pydantic upgrade
    that silently flips the tiebreaker (instead of erroring) trips
    this test rather than silently inverting verdicts in production."""
    from pydantic import ValidationError

    payload = {"verdict": "pass", "result": "fail", "rationale": "x"}

    raised: list[bool] = []
    values: list[str] = []
    for _ in range(2):
        try:
            v = ValidationVerdict.model_validate(payload)
            raised.append(False)
            values.append(v.verdict)
        except ValidationError:
            raised.append(True)

    # Determinism: the two attempts must agree on raise-or-accept and,
    # if they accept, on the resolved value.
    assert raised[0] == raised[1]
    if not raised[0]:
        assert values[0] == values[1]
        assert values[0] in ("pass", "fail")


def test_validation_verdict_invalid_value_via_alias_rejected() -> None:
    """An invalid verdict value supplied via the ``result`` alias still
    fails validation — alias acceptance must not weaken the
    ``Literal["pass", "fail"]`` constraint."""
    with pytest.raises(Exception):  # ValidationError
        ValidationVerdict.model_validate(
            {"result": "maybe", "rationale": "x"}
        )


def test_validation_verdict_missing_both_keys_rejected() -> None:
    """Neither ``verdict`` nor ``result`` present → ValidationError.
    Preserves the existing missing-field error path so silent acceptance
    of a verdictless envelope is still impossible."""
    with pytest.raises(Exception):  # ValidationError
        ValidationVerdict.model_validate({"rationale": "x"})


def test_parse_validation_envelope_accepts_result_alias() -> None:
    """End-to-end: ``_parse_validation_envelope`` returns the same
    ``(verdict, rationale)`` tuple whether the LLM emitted ``result`` or
    ``verdict``. This is the seam where the 116 errored runs were lost."""
    from treadmill_agent.validation_runtime import _parse_validation_envelope

    output_with_result = (
        '```json\n{"result": "pass", "rationale": "ok"}\n```'
    )
    assert _parse_validation_envelope(output_with_result) == ("pass", "ok")

    output_with_verdict = (
        '```json\n{"verdict": "pass", "rationale": "ok"}\n```'
    )
    assert _parse_validation_envelope(output_with_verdict) == ("pass", "ok")


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
