"""Tests for judge_eval module (ADR-0053).

Exercises ``evaluate_judge_prompt`` against a mocked
``treadmill_agent.claude_code.run_claude`` (matching the patch path
used in ``test_validation_runtime``). Verifies score arithmetic,
per-example bookkeeping, and the parse-failure error path.
"""

from __future__ import annotations

import textwrap
from unittest.mock import patch

from treadmill_agent.judge_eval import EvalResult, evaluate_judge_prompt


def _fence(verdict: str, rationale: str = "ok") -> str:
    return textwrap.dedent(f"""
        ```json
        {{"verdict": "{verdict}", "rationale": "{rationale}"}}
        ```
        """)


def test_three_of_four_correct_yields_0_75() -> None:
    examples = [
        {"diff": "d1", "gold_verdict": "pass"},
        {"diff": "d2", "gold_verdict": "fail"},
        {"diff": "d3", "gold_verdict": "pass"},
        {"diff": "d4", "gold_verdict": "pass"},
    ]
    responses = [
        _fence("pass"),  # matches → correct
        _fence("fail"),  # matches → correct
        _fence("fail"),  # MISMATCH (gold=pass) → incorrect
        _fence("pass"),  # matches → correct
    ]

    with patch("treadmill_agent.claude_code.run_claude") as mock_run:
        mock_run.side_effect = responses
        result = evaluate_judge_prompt(
            "judge prompt",
            examples,
            model="claude-haiku-4-5-20251001",
            timeout_seconds=5,
        )

    assert isinstance(result, EvalResult)
    assert result.n == 4
    assert result.correct == 3
    assert result.score == 0.75
    assert len(result.per_example) == 4

    # The mismatched example (index 2) is flagged incorrect but not error.
    assert result.per_example[2]["index"] == 2
    assert result.per_example[2]["correct"] is False
    assert result.per_example[2]["error"] is False
    assert result.per_example[2]["predicted"] == "fail"
    assert result.per_example[2]["gold"] == "pass"

    for i in (0, 1, 3):
        assert result.per_example[i]["correct"] is True
        assert result.per_example[i]["error"] is False


def test_case_insensitive_verdict_match() -> None:
    """``Pass`` vs ``pass`` is a match — comparison is case-insensitive."""
    examples = [{"diff": "d", "gold_verdict": "PASS"}]

    with patch("treadmill_agent.claude_code.run_claude") as mock_run:
        mock_run.return_value = _fence("pass")
        result = evaluate_judge_prompt("p", examples, model="m")

    assert result.correct == 1
    assert result.score == 1.0
    assert result.per_example[0]["correct"] is True


def test_parse_failure_marks_example_error_and_counts_against_score() -> None:
    examples = [
        {"diff": "d1", "gold_verdict": "pass"},
        {"diff": "d2", "gold_verdict": "pass"},
    ]
    responses = [
        _fence("pass"),                 # correct
        "no JSON fence anywhere here",  # unparseable → error
    ]

    with patch("treadmill_agent.claude_code.run_claude") as mock_run:
        mock_run.side_effect = responses
        result = evaluate_judge_prompt("p", examples, model="m")

    assert result.n == 2
    assert result.correct == 1
    assert result.score == 0.5
    assert result.per_example[0]["error"] is False
    assert result.per_example[0]["correct"] is True
    assert result.per_example[1]["error"] is True
    assert result.per_example[1]["correct"] is False
    assert result.per_example[1]["predicted"] is None


def test_empty_examples_returns_zero_score_without_invoking_claude() -> None:
    with patch("treadmill_agent.claude_code.run_claude") as mock_run:
        result = evaluate_judge_prompt("p", [], model="m")

    assert result.n == 0
    assert result.correct == 0
    assert result.score == 0.0
    assert result.per_example == []
    mock_run.assert_not_called()


def test_run_claude_exception_marks_example_error() -> None:
    """An exception from ``run_claude`` (timeout, binary missing, etc.)
    is caught: the example is flagged ``error=True`` and counts against
    the score rather than aborting the whole evaluation."""
    examples = [
        {"diff": "d1", "gold_verdict": "pass"},
        {"diff": "d2", "gold_verdict": "pass"},
    ]

    with patch("treadmill_agent.claude_code.run_claude") as mock_run:
        mock_run.side_effect = [Exception("claude binary missing"), _fence("pass")]
        result = evaluate_judge_prompt("p", examples, model="m")

    assert result.n == 2
    assert result.correct == 1
    assert result.score == 0.5
    assert result.per_example[0]["error"] is True
    assert result.per_example[0]["correct"] is False
    assert result.per_example[0]["predicted"] is None
    assert result.per_example[1]["error"] is False
    assert result.per_example[1]["correct"] is True


def test_prompt_composition_renders_known_and_generic_sections() -> None:
    """``diff`` gets the ``## PR diff`` header (mirrors ``run_llm_judge``);
    a non-standard key gets a generic ``## <key>`` header; the
    ``gold_verdict`` label is never sent to the judge."""
    examples = [
        {
            "diff": "DIFF_BODY",
            "extra_context": "EXTRA_BODY",
            "gold_verdict": "pass",
        }
    ]

    with patch("treadmill_agent.claude_code.run_claude") as mock_run:
        mock_run.return_value = _fence("pass")
        evaluate_judge_prompt("CANDIDATE_PROMPT", examples, model="m")

    prompt = mock_run.call_args.kwargs["prompt"]
    assert "CANDIDATE_PROMPT" in prompt
    assert "## PR diff" in prompt
    assert "DIFF_BODY" in prompt
    assert "## extra_context" in prompt
    assert "EXTRA_BODY" in prompt
    # The gold label must not leak into the prompt the judge sees.
    assert "gold_verdict" not in prompt


def test_general_verdict_vocabulary_for_architect() -> None:
    """``evaluate_judge_prompt`` is verdict-agnostic — it scores
    ``accept-as-is``/``amend`` (architect) the same way it scores
    ``pass``/``fail`` (validator judges)."""
    examples = [
        {"diff": "d1", "gold_verdict": "accept-as-is"},
        {"diff": "d2", "gold_verdict": "amend"},
    ]

    with patch("treadmill_agent.claude_code.run_claude") as mock_run:
        mock_run.side_effect = [_fence("accept-as-is"), _fence("amend")]
        result = evaluate_judge_prompt("p", examples, model="m")

    assert result.correct == 2
    assert result.score == 1.0
