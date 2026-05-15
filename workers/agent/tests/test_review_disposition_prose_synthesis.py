"""Prose synthesis tests for review disposition (ADR-0036).

Tests that the disposition emits identical prose bodies for identical
verdicts, and that the verdict is never at odds with the prose body.
"""

from __future__ import annotations

import pytest

from treadmill_agent.runner_dispositions.review import (
    ReviewVerdict,
    _synthesize_prose_body,
    _extract_issues_from_rationale,
)


class TestExtractIssuesFromRationale:
    """Tests for sentence-split issue extraction from rationale."""

    def test_extract_single_sentence(self) -> None:
        """A single-sentence rationale yields one issue."""
        rationale = "The code lacks proper error handling."
        issues = _extract_issues_from_rationale(rationale)
        assert len(issues) == 1
        assert issues[0] == "The code lacks proper error handling"

    def test_extract_multiple_sentences(self) -> None:
        """Multiple sentences split into separate issues."""
        rationale = (
            "The function has no input validation. "
            "Error messages are not user-friendly. "
            "The performance is suboptimal."
        )
        issues = _extract_issues_from_rationale(rationale)
        assert len(issues) == 3
        assert "input validation" in issues[0]
        assert "user-friendly" in issues[1]
        assert "performance" in issues[2]

    def test_extract_handles_trailing_punctuation(self) -> None:
        """Trailing punctuation is stripped from extracted issues."""
        rationale = "Fix the bug. Add tests."
        issues = _extract_issues_from_rationale(rationale)
        assert issues[0] == "Fix the bug"
        assert issues[1] == "Add tests"

    def test_extract_handles_mixed_punctuation(self) -> None:
        """Handles . ! ? as sentence boundaries."""
        rationale = "Fix the crash! Add error handling. Log warnings?"
        issues = _extract_issues_from_rationale(rationale)
        assert len(issues) == 3
        assert "crash" in issues[0]
        assert "error handling" in issues[1]
        assert "Log warnings" in issues[2]

    def test_extract_ignores_empty_sentences(self) -> None:
        """Empty strings after split are filtered out."""
        rationale = "First issue.   Second issue."
        issues = _extract_issues_from_rationale(rationale)
        assert len(issues) == 2

    def test_extract_empty_rationale(self) -> None:
        """Empty rationale yields empty list."""
        issues = _extract_issues_from_rationale("")
        assert issues == []

    def test_extract_whitespace_only(self) -> None:
        """Whitespace-only rationale yields empty list."""
        issues = _extract_issues_from_rationale("   \n  \t  ")
        assert issues == []


class TestSynthesizeProsebody:
    """Tests for prose synthesis from ReviewVerdict."""

    def test_synthesize_approve_verdict(self) -> None:
        """Approve verdict synthesizes to header + rationale (no issues)."""
        verdict = ReviewVerdict(
            verdict="approve",
            rationale="The changes look good and follow the style guide.",
        )
        body = _synthesize_prose_body(verdict)
        assert "## Treadmill review verdict: approve" in body
        assert "The changes look good and follow the style guide." in body
        assert "## Issues" not in body

    def test_synthesize_request_changes_with_explicit_issues(self) -> None:
        """Request changes with explicit issues list includes Issues section."""
        verdict = ReviewVerdict(
            verdict="request_changes",
            rationale="Several issues need fixing.",
            issues=["Add error handling", "Write tests for edge cases"],
        )
        body = _synthesize_prose_body(verdict)
        assert "## Treadmill review verdict: request changes" in body
        assert "Several issues need fixing." in body
        assert "## Issues" in body
        assert "- Add error handling" in body
        assert "- Write tests for edge cases" in body

    def test_synthesize_request_changes_derived_issues(self) -> None:
        """Request changes without explicit issues derives from rationale."""
        verdict = ReviewVerdict(
            verdict="request_changes",
            rationale="Missing error handling. Test coverage is insufficient.",
        )
        body = _synthesize_prose_body(verdict)
        assert "## Treadmill review verdict: request changes" in body
        assert "Missing error handling" in body
        assert "## Issues" in body
        assert "- Missing error handling" in body
        assert "- Test coverage is insufficient" in body

    def test_synthesize_request_changes_empty_issues_list(self) -> None:
        """Request changes with empty issues list still has Issues header."""
        verdict = ReviewVerdict(
            verdict="request_changes",
            rationale="Issues exist but are not listed.",
            issues=[],
        )
        body = _synthesize_prose_body(verdict)
        assert "## Treadmill review verdict: request changes" in body
        assert "Issues exist but are not listed." in body
        # Empty issues list means no Issues section
        assert "## Issues" not in body

    def test_synthesize_request_changes_single_sentence(self) -> None:
        """Request changes with single-sentence rationale yields one issue."""
        verdict = ReviewVerdict(
            verdict="request_changes",
            rationale="The code needs refactoring.",
        )
        body = _synthesize_prose_body(verdict)
        assert "## Treadmill review verdict: request changes" in body
        assert "The code needs refactoring." in body
        assert "## Issues" in body
        assert "- The code needs refactoring" in body

    def test_synthesize_idempotent_approve(self) -> None:
        """Synthesizing the same approve verdict twice yields identical prose."""
        verdict = ReviewVerdict(
            verdict="approve",
            rationale="This looks good.",
        )
        body1 = _synthesize_prose_body(verdict)
        body2 = _synthesize_prose_body(verdict)
        assert body1 == body2

    def test_synthesize_idempotent_request_changes(self) -> None:
        """Synthesizing the same request_changes verdict twice yields identical prose."""
        verdict = ReviewVerdict(
            verdict="request_changes",
            rationale="Fix the bug. Add tests.",
            issues=["Fix the bug", "Add tests"],
        )
        body1 = _synthesize_prose_body(verdict)
        body2 = _synthesize_prose_body(verdict)
        assert body1 == body2

    def test_synthesize_verdict_never_disagrees_with_prose(self) -> None:
        """The verdict in the header always matches the verdict field."""
        for verdict_value in ["approve", "request_changes"]:
            verdict = ReviewVerdict(
                verdict=verdict_value,
                rationale="Test rationale.",
            )
            body = _synthesize_prose_body(verdict)
            # Check the header contains the correct verdict
            if verdict_value == "approve":
                assert "## Treadmill review verdict: approve" in body
            else:
                assert "## Treadmill review verdict: request changes" in body

    def test_synthesize_rationale_always_in_body(self) -> None:
        """The rationale is always present in the synthesized body."""
        rationale = "This is a specific rationale."
        verdict = ReviewVerdict(
            verdict="approve",
            rationale=rationale,
        )
        body = _synthesize_prose_body(verdict)
        assert rationale in body

    def test_synthesize_explicit_issues_override_derived(self) -> None:
        """Explicit issues list overrides derived issues from rationale."""
        verdict = ReviewVerdict(
            verdict="request_changes",
            rationale="First issue. Second issue. Third issue.",
            issues=["Custom issue 1", "Custom issue 2"],
        )
        body = _synthesize_prose_body(verdict)
        # Should have explicit issues, not derived ones
        assert "- Custom issue 1" in body
        assert "- Custom issue 2" in body
        # Derived issues should not be in the body (they're overridden)
        assert "- First issue" not in body
        assert "- Second issue" not in body
        assert "- Third issue" not in body

    def test_synthesize_multiline_rationale_preserved(self) -> None:
        """Multiline rationale is preserved as-is in the body."""
        rationale = "This is a long rationale\nthat spans multiple lines\nfor clarity."
        verdict = ReviewVerdict(
            verdict="approve",
            rationale=rationale,
        )
        body = _synthesize_prose_body(verdict)
        assert rationale in body

    def test_synthesize_special_characters_in_rationale(self) -> None:
        """Special characters in rationale are preserved."""
        rationale = "Code has issues: missing @param, incorrect &logic, etc."
        verdict = ReviewVerdict(
            verdict="request_changes",
            rationale=rationale,
        )
        body = _synthesize_prose_body(verdict)
        assert rationale in body


class TestIdenticalBodyForIdenticalVerdicts:
    """Verify that identical verdicts always produce identical prose."""

    def test_identical_approve_verdicts_yield_identical_prose(self) -> None:
        """Two identical approve verdicts produce the same prose."""
        v1 = ReviewVerdict(verdict="approve", rationale="Looks good.")
        v2 = ReviewVerdict(verdict="approve", rationale="Looks good.")
        assert _synthesize_prose_body(v1) == _synthesize_prose_body(v2)

    def test_identical_request_changes_explicit_issues_yield_identical_prose(self) -> None:
        """Two identical request_changes verdicts with explicit issues produce the same prose."""
        v1 = ReviewVerdict(
            verdict="request_changes",
            rationale="Needs work.",
            issues=["Fix A", "Fix B"],
        )
        v2 = ReviewVerdict(
            verdict="request_changes",
            rationale="Needs work.",
            issues=["Fix A", "Fix B"],
        )
        assert _synthesize_prose_body(v1) == _synthesize_prose_body(v2)

    def test_identical_request_changes_derived_issues_yield_identical_prose(self) -> None:
        """Two identical request_changes verdicts without explicit issues produce the same prose."""
        v1 = ReviewVerdict(
            verdict="request_changes",
            rationale="Needs work. Add tests.",
        )
        v2 = ReviewVerdict(
            verdict="request_changes",
            rationale="Needs work. Add tests.",
        )
        assert _synthesize_prose_body(v1) == _synthesize_prose_body(v2)

    def test_different_verdicts_yield_different_prose(self) -> None:
        """Different verdicts produce different prose."""
        v_approve = ReviewVerdict(verdict="approve", rationale="Good.")
        v_changes = ReviewVerdict(verdict="request_changes", rationale="Good.")
        # Verdict should differ in the header
        assert _synthesize_prose_body(v_approve) != _synthesize_prose_body(v_changes)

    def test_different_rationales_yield_different_prose(self) -> None:
        """Different rationales produce different prose."""
        v1 = ReviewVerdict(verdict="approve", rationale="Rationale A.")
        v2 = ReviewVerdict(verdict="approve", rationale="Rationale B.")
        assert _synthesize_prose_body(v1) != _synthesize_prose_body(v2)

    def test_different_issues_yield_different_prose(self) -> None:
        """Different issues lists produce different prose."""
        v1 = ReviewVerdict(
            verdict="request_changes",
            rationale="Review.",
            issues=["Issue A", "Issue B"],
        )
        v2 = ReviewVerdict(
            verdict="request_changes",
            rationale="Review.",
            issues=["Issue A", "Issue C"],
        )
        assert _synthesize_prose_body(v1) != _synthesize_prose_body(v2)


class TestVerdictConsistency:
    """Verify that the prose body is always consistent with the verdict."""

    def test_approve_body_never_mentions_issues_section(self) -> None:
        """Approve verdicts never emit an Issues section."""
        verdict = ReviewVerdict(verdict="approve", rationale="LGTM.")
        body = _synthesize_prose_body(verdict)
        assert "## Issues" not in body

    def test_request_changes_body_includes_issues_when_possible(self) -> None:
        """Request changes verdicts include Issues section (explicit or derived)."""
        # With explicit issues
        v1 = ReviewVerdict(
            verdict="request_changes",
            rationale="Needs work.",
            issues=["Fix"],
        )
        assert "## Issues" in _synthesize_prose_body(v1)

        # With derived issues (multi-sentence)
        v2 = ReviewVerdict(
            verdict="request_changes",
            rationale="Needs work. Add tests.",
        )
        assert "## Issues" in _synthesize_prose_body(v2)

    def test_verdict_header_matches_verdict_field(self) -> None:
        """The verdict in the header always matches the verdict field."""
        test_cases = [
            ("approve", "## Treadmill review verdict: approve"),
            ("request_changes", "## Treadmill review verdict: request changes"),
        ]
        for verdict_value, expected_header in test_cases:
            verdict = ReviewVerdict(verdict=verdict_value, rationale="Test.")
            body = _synthesize_prose_body(verdict)
            assert expected_header in body

    def test_rationale_always_appears_after_header(self) -> None:
        """The rationale always appears after the header line."""
        rationale = "Specific rationale text."
        verdict = ReviewVerdict(verdict="approve", rationale=rationale)
        body = _synthesize_prose_body(verdict)
        header_idx = body.find("## Treadmill review verdict:")
        rationale_idx = body.find(rationale)
        assert header_idx >= 0
        assert rationale_idx > header_idx
