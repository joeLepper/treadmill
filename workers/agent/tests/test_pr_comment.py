"""Tests for structured PR comment generation (ADR-0033)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from treadmill_agent import gh, pr_comment


class TestValidateSections:
    """Tests for section validation."""

    def test_valid_body_with_all_sections(self) -> None:
        """Validate a well-formed body with all required sections."""
        body = """Architecture resolve hit the retry cap after 5 attempts.
Manual review required.

Action items:
- Review the Claude output in the PR
- Decide if the gap is acceptable

See: https://linear.com/issue/T-123"""
        sections = pr_comment._validate_sections(body)
        assert "Architecture resolve" in sections["summary"]
        assert "Review the Claude" in sections["action_items"]
        assert "https://linear.com" in sections["see"]

    def test_case_insensitive_section_headers(self) -> None:
        """Section headers are case-insensitive."""
        body = """Summary text here.

ACTION ITEMS:
- Item 1

SEE: link"""
        sections = pr_comment._validate_sections(body)
        assert sections["summary"].strip()
        assert sections["action_items"].strip()
        assert sections["see"].strip()

    def test_missing_action_items_section(self) -> None:
        """Reject body without Action items section."""
        body = """Summary here.

See: link"""
        with pytest.raises(
            pr_comment.PrCommentError,
            match="Action items",
        ):
            pr_comment._validate_sections(body)

    def test_missing_see_section(self) -> None:
        """Reject body without See section."""
        body = """Summary here.

Action items:
- Item 1"""
        with pytest.raises(pr_comment.PrCommentError, match="See"):
            pr_comment._validate_sections(body)

    def test_empty_summary(self) -> None:
        """Reject body with empty summary."""
        body = """

Action items:
- Item 1

See: link"""
        with pytest.raises(
            pr_comment.PrCommentError,
            match="summary section cannot be empty",
        ):
            pr_comment._validate_sections(body)

    def test_empty_action_items(self) -> None:
        """Reject body with empty action items section."""
        body = """Summary here.

Action items:

See: link"""
        with pytest.raises(
            pr_comment.PrCommentError,
            match="action items section cannot be empty",
        ):
            pr_comment._validate_sections(body)

    def test_empty_see_section(self) -> None:
        """Reject body with empty See section."""
        body = """Summary here.

Action items:
- Item 1

See:"""
        with pytest.raises(
            pr_comment.PrCommentError,
            match="see section cannot be empty",
        ):
            pr_comment._validate_sections(body)


class TestRenderComment:
    """Tests for comment rendering."""

    def test_render_includes_prefix(self) -> None:
        """Rendered comment includes the structured prefix."""
        sections = {
            "summary": "Summary text",
            "action_items": "- Action 1",
            "see": "https://example.com",
        }
        rendered = pr_comment._render_comment(
            "[treadmill:wf-test:uncertain]",
            sections,
        )
        assert "[treadmill:wf-test:uncertain]" in rendered

    def test_render_includes_all_sections(self) -> None:
        """Rendered comment includes all required sections."""
        sections = {
            "summary": "Architecture gap detected",
            "action_items": "- Review manually\n- Approve or rework",
            "see": "https://linear.com/issue/T-123",
        }
        rendered = pr_comment._render_comment(
            "[treadmill:wf-resolve:capped]",
            sections,
        )
        assert "Architecture gap" in rendered
        assert "**Action items**" in rendered
        assert "Review manually" in rendered
        assert "**See**" in rendered
        assert "https://linear.com" in rendered

    def test_render_format_structure(self) -> None:
        """Rendered comment has the expected structural format."""
        sections = {
            "summary": "Summary",
            "action_items": "- Item",
            "see": "Link",
        }
        rendered = pr_comment._render_comment(
            "[treadmill:wf-test:signal]",
            sections,
        )
        lines = rendered.split("\n")
        assert lines[0] == "[treadmill:wf-test:signal]"
        assert lines[1] == ""  # blank line after prefix
        assert "Summary" in rendered


class TestLeaveprComment:
    """Tests for the main leave_pr_comment function."""

    @patch("treadmill_agent.gh.pr_comment")
    def test_valid_comment_posts_successfully(
        self, mock_gh_comment: MagicMock,
    ) -> None:
        """Valid comment with all sections posts successfully."""
        body = """Retry cap exhausted.

Action items:
- Review output
- Approve

See: https://example.com"""

        pr_comment.leave_pr_comment(
            workflow_id="wf-resolve",
            signal="capped",
            body=body,
            pr_number=42,
        )

        mock_gh_comment.assert_called_once()
        call_args = mock_gh_comment.call_args
        assert call_args[0][0] == 42
        assert "[treadmill:wf-resolve:capped]" in call_args[1]["body"]
        assert "Retry cap" in call_args[1]["body"]

    @patch("treadmill_agent.gh.pr_comment")
    def test_prefix_format_correct(
        self, mock_gh_comment: MagicMock,
    ) -> None:
        """Prefix is formatted as [treadmill:<workflow>:<signal>]."""
        body = """Test summary.

Action items:
- Test

See: test"""

        pr_comment.leave_pr_comment(
            workflow_id="wf-doc-amend",
            signal="accept-as-is",
            body=body,
            pr_number=123,
        )

        rendered = mock_gh_comment.call_args[1]["body"]
        assert "[treadmill:wf-doc-amend:accept-as-is]" in rendered

    @patch("treadmill_agent.gh.pr_comment")
    def test_different_workflow_ids_in_prefix(
        self, mock_gh_comment: MagicMock,
    ) -> None:
        """Prefix reflects the provided workflow ID."""
        body = """Summary.

Action items:
- Item

See: link"""

        for workflow_id in ["wf-arch-resolve", "wf-doc-amend", "custom-wf"]:
            mock_gh_comment.reset_mock()
            pr_comment.leave_pr_comment(
                workflow_id=workflow_id,
                signal="test-signal",
                body=body,
                pr_number=1,
            )
            rendered = mock_gh_comment.call_args[1]["body"]
            assert f"[treadmill:{workflow_id}:test-signal]" in rendered

    @patch("treadmill_agent.gh.pr_comment")
    def test_different_signal_types_in_prefix(
        self, mock_gh_comment: MagicMock,
    ) -> None:
        """Prefix reflects the provided signal type."""
        body = """Summary.

Action items:
- Item

See: link"""

        for signal in ["capped", "accept-as-is", "class-c-gap"]:
            mock_gh_comment.reset_mock()
            pr_comment.leave_pr_comment(
                workflow_id="wf-test",
                signal=signal,
                body=body,
                pr_number=1,
            )
            rendered = mock_gh_comment.call_args[1]["body"]
            assert f"[treadmill:wf-test:{signal}]" in rendered

    def test_empty_body_raises_error(self) -> None:
        """Empty body raises PrCommentError."""
        with pytest.raises(pr_comment.PrCommentError, match="empty"):
            pr_comment.leave_pr_comment(
                workflow_id="wf-test",
                signal="test",
                body="",
                pr_number=1,
            )

    def test_missing_action_items_raises_error(self) -> None:
        """Body without Action items section raises PrCommentError."""
        body = """Summary here.

See: link"""
        with pytest.raises(pr_comment.PrCommentError, match="Action items"):
            pr_comment.leave_pr_comment(
                workflow_id="wf-test",
                signal="test",
                body=body,
                pr_number=1,
            )

    def test_missing_see_section_raises_error(self) -> None:
        """Body without See section raises PrCommentError."""
        body = """Summary here.

Action items:
- Item 1"""
        with pytest.raises(pr_comment.PrCommentError, match="See"):
            pr_comment.leave_pr_comment(
                workflow_id="wf-test",
                signal="test",
                body=body,
                pr_number=1,
            )

    @patch("treadmill_agent.gh.pr_comment")
    def test_gh_cli_error_wrapped_in_pr_comment_error(
        self, mock_gh_comment: MagicMock,
    ) -> None:
        """GhCliError from gh module is wrapped in PrCommentError."""
        mock_gh_comment.side_effect = gh.GhCliError("gh CLI failed")
        body = """Summary.

Action items:
- Item

See: link"""
        with pytest.raises(
            pr_comment.PrCommentError,
            match="failed to post PR comment",
        ):
            pr_comment.leave_pr_comment(
                workflow_id="wf-test",
                signal="test",
                body=body,
                pr_number=1,
            )

    @patch("treadmill_agent.gh.pr_comment")
    def test_pr_number_passed_to_gh(
        self, mock_gh_comment: MagicMock,
    ) -> None:
        """PR number is passed correctly to gh.pr_comment."""
        body = """Summary.

Action items:
- Item

See: link"""

        for pr_num in [1, 42, 999]:
            mock_gh_comment.reset_mock()
            pr_comment.leave_pr_comment(
                workflow_id="wf-test",
                signal="test",
                body=body,
                pr_number=pr_num,
            )
            assert mock_gh_comment.call_args[0][0] == pr_num

    @patch("treadmill_agent.gh.pr_comment")
    def test_cwd_parameter_passed_through(
        self, mock_gh_comment: MagicMock,
    ) -> None:
        """cwd parameter is passed through to gh.pr_comment."""
        from pathlib import Path

        body = """Summary.

Action items:
- Item

See: link"""
        test_cwd = Path("/test/path")

        pr_comment.leave_pr_comment(
            workflow_id="wf-test",
            signal="test",
            body=body,
            pr_number=1,
            cwd=test_cwd,
        )

        assert mock_gh_comment.call_args[1]["cwd"] == test_cwd

    @patch("treadmill_agent.gh.pr_comment")
    def test_multiline_body_sections(
        self, mock_gh_comment: MagicMock,
    ) -> None:
        """Body with multiline sections is handled correctly."""
        body = """Architecture resolve produced an accept-as-is verdict.
Operator confirmation requested.

Action items:
- Review the Claude Code output in the PR
- Examine the architect's reasoning
- Either approve the gap or request rework
- Trigger redispatch if needed

See:
- ADR-0032 for architect verdicts
- https://linear.com/issue/T-456
- Previous attempts in PR history"""

        pr_comment.leave_pr_comment(
            workflow_id="wf-architecture-resolve",
            signal="accept-as-is",
            body=body,
            pr_number=789,
        )

        rendered = mock_gh_comment.call_args[1]["body"]
        assert "Architecture resolve" in rendered
        assert "Review the Claude Code" in rendered
        assert "ADR-0032" in rendered
        assert "[treadmill:wf-architecture-resolve:accept-as-is]" in rendered
