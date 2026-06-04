"""Deterministic pre-resolver for additive-list-head merge conflicts.

Triage finding 71ed396b (PR #144): markdown list-head collisions in
AGENT.md / MEMORY.md are common and fully deterministic to merge.
This module resolves them before the conflict-analyzer role runs,
saving latency and tokens when all conflicts match the pattern.

The four-condition pattern:
  1. Both sides purely additive — both conflict sections are non-empty.
  2. Every added line is a markdown list item or heading.
  3. The pre-conflict anchor line is a list item, heading, or blank
     (blank covers the ``## Heading\\n\\n<<<`` structure common in AGENT.md).
  4. The post-conflict anchor line is blank, EOF, or a list item.

Resolution when all four conditions hold: HEAD's additions concatenated
with the incoming additions, preserving the post-anchor context intact.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class PreresolveStatus(str, Enum):
    resolved = "resolved"
    skipped_not_unmerged = "skipped_not_unmerged"
    unresolved_not_additive = "unresolved_not_additive"
    unresolved_not_list = "unresolved_not_list"
    unresolved_anchor_mismatch = "unresolved_anchor_mismatch"
    unresolved_multiple_patterns = "unresolved_multiple_patterns"


@dataclass(frozen=True)
class Hunk:
    start_line: int       # 0-based index of the <<<<<<< line
    end_line: int         # 0-based index of the >>>>>>> line
    head_lines: tuple[str, ...]
    incoming_lines: tuple[str, ...]
    pre_anchor: str | None   # line immediately before <<<<<<<; None at BOF
    post_anchor: str | None  # line immediately after >>>>>>>;  None at EOF


@dataclass(frozen=True)
class FileResult:
    path: Path
    status: PreresolveStatus
    hunks_resolved: int
    hunks_total: int


@dataclass(frozen=True)
class PreresolveSummary:
    results: tuple[FileResult, ...]

    @property
    def all_resolved(self) -> bool:
        if not self.results:
            return False
        return all(r.status == PreresolveStatus.resolved for r in self.results)

    @property
    def resolved_count(self) -> int:
        return sum(1 for r in self.results if r.status == PreresolveStatus.resolved)


def resolve_additive_list_head(repo_dir: Path) -> PreresolveSummary:
    """Scan the working tree under repo_dir for unmerged files.

    For each unmerged file, attempt the additive-list-head resolution.
    On success, write the resolved content and ``git add`` the file.
    Returns per-file status in a ``PreresolveSummary``.
    """
    unmerged = _unmerged_files(repo_dir)
    if not unmerged:
        return PreresolveSummary(results=())

    results = [_resolve_file(repo_dir, path) for path in unmerged]
    return PreresolveSummary(results=tuple(results))


# ── internals ─────────────────────────────────────────────────────────────────


def _unmerged_files(repo_dir: Path) -> list[Path]:
    """Return absolute paths of unmerged files in the working tree."""
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "diff", "--name-only", "--diff-filter=U"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    return [
        repo_dir / line.strip()
        for line in result.stdout.splitlines()
        if line.strip()
    ]


def _resolve_file(repo_dir: Path, path: Path) -> FileResult:
    """Attempt to resolve all conflict hunks in a single file."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    hunks = _parse_hunks(text)

    if not hunks:
        return FileResult(
            path=path, status=PreresolveStatus.skipped_not_unmerged,
            hunks_resolved=0, hunks_total=0,
        )

    hunk_resolutions: list[tuple[Hunk, tuple[str, ...] | None, PreresolveStatus]] = []
    for hunk in hunks:
        status = _classify_hunk(hunk)
        resolved = _concatenate(hunk) if status == PreresolveStatus.resolved else None
        hunk_resolutions.append((hunk, resolved, status))

    hunks_total = len(hunks)
    hunks_resolved = sum(1 for _, r, _ in hunk_resolutions if r is not None)

    if hunks_resolved > 0:
        _write_resolved(
            repo_dir, path, lines, hunk_resolutions,
            stage=(hunks_resolved == hunks_total),
        )

    if hunks_resolved == hunks_total:
        return FileResult(
            path=path, status=PreresolveStatus.resolved,
            hunks_resolved=hunks_resolved, hunks_total=hunks_total,
        )

    # Determine the file-level failure status from unresolved hunks.
    unresolved_statuses = [s for _, r, s in hunk_resolutions if r is None]
    unique = set(unresolved_statuses)
    file_status = (
        PreresolveStatus.unresolved_multiple_patterns
        if len(unique) > 1
        else unresolved_statuses[0]
    )
    return FileResult(
        path=path, status=file_status,
        hunks_resolved=hunks_resolved, hunks_total=hunks_total,
    )


def _parse_hunks(text: str) -> list[Hunk]:
    """Parse all conflict hunks from file text.

    Each hunk carries the HEAD lines, incoming lines, and the surrounding
    pre/post anchor lines.  The parser looks for the standard git three-part
    conflict marker triple: ``<<<<<<<`` / ``=======`` / ``>>>>>>>``.
    """
    lines = text.splitlines(keepends=True)
    hunks: list[Hunk] = []
    i = 0

    while i < len(lines):
        if not lines[i].startswith("<<<<<<<"):
            i += 1
            continue

        start_line = i
        pre_anchor = lines[i - 1].rstrip("\n\r") if i > 0 else None

        # Collect HEAD lines (between <<<<<<< and =======)
        head_lines: list[str] = []
        i += 1
        while i < len(lines) and not lines[i].startswith("======="):
            head_lines.append(lines[i].rstrip("\n\r"))
            i += 1
        if i >= len(lines):
            break  # malformed — stop parsing

        # Skip the ======= separator
        i += 1

        # Collect incoming lines (between ======= and >>>>>>>)
        incoming_lines: list[str] = []
        while i < len(lines) and not lines[i].startswith(">>>>>>>"):
            incoming_lines.append(lines[i].rstrip("\n\r"))
            i += 1
        if i >= len(lines):
            break  # malformed

        end_line = i
        post_anchor = lines[i + 1].rstrip("\n\r") if i + 1 < len(lines) else None

        hunks.append(Hunk(
            start_line=start_line,
            end_line=end_line,
            head_lines=tuple(head_lines),
            incoming_lines=tuple(incoming_lines),
            pre_anchor=pre_anchor,
            post_anchor=post_anchor,
        ))
        i += 1  # advance past the >>>>>>> line

    return hunks


def _classify_hunk(hunk: Hunk) -> PreresolveStatus:
    """Return the resolution status for a single hunk.

    Checks the four conditions in order; returns the first failing
    status, or ``resolved`` when all four pass.
    """
    if not _both_pure_additive(hunk):
        return PreresolveStatus.unresolved_not_additive
    if not all(_is_list_item(ln) for ln in hunk.head_lines + hunk.incoming_lines if ln.strip()):
        return PreresolveStatus.unresolved_not_list
    if not _anchor_pre_is_list_or_heading(hunk):
        return PreresolveStatus.unresolved_anchor_mismatch
    if not _anchor_post_is_list_or_blank_or_eof(hunk):
        return PreresolveStatus.unresolved_anchor_mismatch
    return PreresolveStatus.resolved


def _both_pure_additive(hunk: Hunk) -> bool:
    """Return True when both conflict sides have at least one non-blank line.

    An empty side indicates a deletion (one branch removed content that
    the other branch added or kept), which is not a purely-additive
    collision.
    """
    head_has = any(ln.strip() for ln in hunk.head_lines)
    incoming_has = any(ln.strip() for ln in hunk.incoming_lines)
    return head_has and incoming_has


def _is_list_item(line: str) -> bool:
    """Return True when line is a markdown list item or heading."""
    s = line.strip()
    if not s:
        return False
    # Unordered list markers: -, *, +
    if s[0] in "-*+" and len(s) > 1 and s[1] == " ":
        return True
    # Ordered list: digit(s) followed by . or ) and a space
    i = 0
    while i < len(s) and s[i].isdigit():
        i += 1
    if i > 0 and i < len(s) and s[i] in ".)" and i + 1 < len(s) and s[i + 1] == " ":
        return True
    # ATX headings
    if s.startswith("#"):
        return True
    return False


def _anchor_pre_is_list_or_heading(hunk: Hunk) -> bool:
    """Return True when the pre-anchor line is a list item, heading, or blank.

    A blank pre-anchor is accepted because the ``## Section\\n\\n<<<`` pattern
    (heading, blank line, then conflict markers) is the canonical AGENT.md
    list-head shape — the blank line separates the section heading from the
    list that follows it.
    """
    if hunk.pre_anchor is None:
        return True  # BOF — no context to reject
    if not hunk.pre_anchor.strip():
        return True  # blank line preceding the list start
    return _is_list_item(hunk.pre_anchor)


def _anchor_post_is_list_or_blank_or_eof(hunk: Hunk) -> bool:
    """Return True when the post-anchor is blank, EOF, or a list item."""
    if hunk.post_anchor is None:
        return True  # EOF
    if not hunk.post_anchor.strip():
        return True  # blank line
    return _is_list_item(hunk.post_anchor)


def _concatenate(hunk: Hunk) -> tuple[str, ...]:
    """Merge HEAD's additions followed by the incoming additions."""
    return tuple(hunk.head_lines) + tuple(hunk.incoming_lines)


def _write_resolved(
    repo_dir: Path,
    path: Path,
    lines: list[str],
    hunk_resolutions: list[tuple[Hunk, tuple[str, ...] | None, PreresolveStatus]],
    *,
    stage: bool,
) -> None:
    """Rewrite path with resolved hunks applied and optionally ``git add`` it.

    Processes hunks in reverse order so earlier line indices stay valid as
    later hunks are spliced out.  Unresolved hunks (``resolved is None``)
    are left with their conflict markers intact.
    """
    result = list(lines)
    for hunk, resolved, _ in reversed(hunk_resolutions):
        if resolved is None:
            continue
        replacement = [ln + "\n" for ln in resolved]
        result[hunk.start_line:hunk.end_line + 1] = replacement

    path.write_text("".join(result), encoding="utf-8")

    if stage:
        subprocess.run(
            ["git", "-C", str(repo_dir), "add", str(path)],
            capture_output=True, text=True, check=True,
        )
