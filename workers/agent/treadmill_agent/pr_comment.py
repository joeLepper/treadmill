"""Structured PR comment generation per ADR-0033 §PR comments.

The disposition layer emits PR comments whenever a workflow exhausts its
retry cap, returns uncertain/accept-as-is verdicts, or surfaces Class C
gaps (ADR-0030 §4). Comments carry a machine-greppable prefix for later
archaeology, required sections for human readability, and action items
for operator adjudication.

This module provides the template and rendering — the dispatch wiring
(which decides *when* to emit) lives in the runner's disposition handlers.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from treadmill_agent import gh

if TYPE_CHECKING:
    pass

import logging

logger = logging.getLogger("treadmill.agent.pr_comment")


class PrCommentError(RuntimeError):
    """Raised when PR comment generation or posting fails."""


def leave_pr_comment(
    workflow_id: str,
    signal: str,
    body: str,
    pr_number: int,
    cwd: Path | None = None,
) -> None:
    """Post a structured PR comment with machine-greppable prefix.

    Per ADR-0033 §PR comments, the comment carries:

      1. Structured prefix: ``[treadmill:<workflow_id>:<signal>]``
      2. Required sections: Summary, Action items, See
      3. A single-paragraph human summary (from the supplied body)

    The body parameter should be plain text with newline-separated
    sections:
      - First paragraph: human summary
      - "Action items:" followed by bulleted items
      - "See:" followed by links or references

    Example body:

        Architecture resolve hit the retry cap after 5 attempts. \
Manual review required.

        Action items:
        - Review the Claude output in the PR
        - Decide if the gap is acceptable or needs rework

        See: https://linear.com/issue/T-123

    The function validates that required sections are present and calls
    ``gh pr comment`` to post the comment. Auth comes from the ``gh``
    keyring (same as the existing ``gh.pr_comment`` function).

    Raises ``PrCommentError`` on missing sections or if the ``gh`` CLI
    fails; ``gh.GhCliError`` details are wrapped for consistency.
    """
    prefix = f"[treadmill:{workflow_id}:{signal}]"

    if not body or not body.strip():
        raise PrCommentError("body cannot be empty")

    sections = _validate_sections(body)
    rendered = _render_comment(prefix, sections)

    try:
        gh.pr_comment(pr_number, body=rendered, cwd=cwd)
    except gh.GhCliError as e:
        raise PrCommentError(f"failed to post PR comment: {e}") from e


def _validate_sections(body: str) -> dict[str, str]:
    """Validate that body contains required sections.

    Required sections (case-insensitive): Summary, Action items, See.
    Returns a dict mapping section names to content.

    The body format is expected to be:
      - Plain-text summary (first paragraph)
      - "Action items:" header followed by bullet points
      - "See:" header followed by links/references

    Per Q33.c, we validate section presence but allow flexible
    formatting within each section (mirrors AGENT.md schema discipline).
    """
    body_lower = body.lower()

    if "action items:" not in body_lower:
        raise PrCommentError(
            'body must contain "Action items:" section (case-insensitive)'
        )
    if "see:" not in body_lower:
        raise PrCommentError(
            'body must contain "See:" section (case-insensitive)'
        )

    lines = body.split("\n")
    sections = {"summary": "", "action_items": "", "see": ""}

    current_section = "summary"
    summary_lines = []

    for line in lines:
        line_lower = line.lower().strip()

        if line_lower.startswith("action items:"):
            # End summary section, start action items
            if summary_lines:
                sections["summary"] = "\n".join(summary_lines).strip()
            summary_lines = []
            current_section = "action_items"
            # Capture any inline content after "Action items:"
            inline = line.split(":", 1)[1].strip()
            if inline:
                summary_lines.append(inline)
        elif line_lower.startswith("see:"):
            # End action items section, start see
            sections["action_items"] = "\n".join(summary_lines).strip()
            summary_lines = []
            current_section = "see"
            # Capture any inline content after "See:"
            inline = line.split(":", 1)[1].strip()
            if inline:
                summary_lines.append(inline)
        elif current_section == "summary" and line.strip():
            summary_lines.append(line)
        elif current_section in ("action_items", "see"):
            summary_lines.append(line)

    # Capture the final section
    if current_section == "action_items":
        sections["action_items"] = "\n".join(summary_lines).strip()
    elif current_section == "see":
        sections["see"] = "\n".join(summary_lines).strip()

    if not sections["summary"]:
        raise PrCommentError("summary section cannot be empty")
    if not sections["action_items"]:
        raise PrCommentError("action items section cannot be empty")
    if not sections["see"]:
        raise PrCommentError("see section cannot be empty")

    return sections


def _render_comment(prefix: str, sections: dict[str, str]) -> str:
    """Render a structured PR comment from prefix and sections.

    Returns the formatted comment body for posting via ``gh pr comment``.
    """
    lines = [
        prefix,
        "",
        sections["summary"],
        "",
        "**Action items**",
        sections["action_items"],
        "",
        "**See**",
        sections["see"],
    ]
    return "\n".join(lines)
