"""Unit tests for operator_note injection into system prompt (ADR-0081).

Tests the prompt injection seam when operator_note is non-null and
worker_hints_enabled is true.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from treadmill_agent.claude_code import _inject_operator_hint


def test_inject_operator_hint_basic() -> None:
    """Injecting a hint appends the envelope to system prompt."""
    system_prompt = "You are a code assistant."
    note = "Fix the failing test in utils.py"

    result = _inject_operator_hint(system_prompt, note)

    assert "## Operator hint" in result
    assert note in result
    assert "End operator hint" in result
    assert "Take it seriously but verify before acting" in result


def test_inject_operator_hint_preserves_original() -> None:
    """The original system_prompt is preserved as prefix."""
    original = "Original system prompt text"
    note = "Test hint"

    result = _inject_operator_hint(original, note)

    assert result.startswith(original)
    assert "## Operator hint" in result


def test_inject_operator_hint_multiline_note() -> None:
    """Multi-line notes are preserved verbatim."""
    system_prompt = "System"
    note = "Line 1\nLine 2\nLine 3"

    result = _inject_operator_hint(system_prompt, note)

    assert "Line 1\nLine 2\nLine 3" in result


def test_inject_operator_hint_special_characters() -> None:
    """Special characters in notes are handled correctly."""
    system_prompt = "System"
    note = "Code example: `arr.forEach(x => x.map(fn))`"

    result = _inject_operator_hint(system_prompt, note)

    assert note in result


def test_inject_operator_hint_empty_system_prompt() -> None:
    """Hint injection works with empty system prompt."""
    result = _inject_operator_hint("", "test hint")

    assert "## Operator hint" in result
    assert "test hint" in result
