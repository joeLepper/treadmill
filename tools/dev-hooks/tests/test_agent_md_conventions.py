"""Repo-wide AGENT.md convention pins (task 986c5cf6).

The Recent-changes prepend convention was a conflict factory: every
in-flight PR inserted its entry at the same anchor (the line after
``## Recent changes``), so every merge re-conflicted every open PR —
three same-day cascades on 2026-06-12. New entries are per-PR fragment
files under ``<component>/agent-changes/`` (decision record:
``docs/learnings/2026-06-12-agent-md-recent-changes-is-a-conflict-factory.md``;
authoring flow: ``docs/agent-md-schema.md``).

These tests walk the repo, so any NEW AGENT.md with a Recent-changes
section must also carry the fragment pointer, and any fragment added
anywhere must be well-formed. They live in dev-hooks because this is
the repo-hygiene component and its CI job checks out the full tree.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# The load-bearing phrase every Recent-changes section must carry —
# whitespace-normalized comparison so hard line wraps don't matter.
POINTER_PHRASE = "New entries are PER-PR FRAGMENT FILES, not prepends"

# Slug must BEGIN with the task-id short form (8 hex) or the PR number
# so path uniqueness is inherited from the allocator — a freeform slug
# lets two same-day same-component PRs add/add-collide on an identical
# path (worker-1's #349 review). NOTE: a bare digit-after-date check
# would reject letter-leading hex task ids (e.g. fb3d593f), so the pin
# accepts exactly the two mandated forms.
FRAGMENT_NAME = re.compile(
    r"^\d{4}-\d{2}-\d{2}-(?:[0-9a-f]{8}|\d{1,6})(?:-[a-z0-9][a-z0-9-]*)?\.md$"
)

_SKIP_PARTS = {"node_modules", ".git", ".venv", "__pycache__"}


def _repo_files(pattern: str) -> list[Path]:
    return [
        p
        for p in REPO_ROOT.rglob(pattern)
        if not (_SKIP_PARTS & set(p.parts))
    ]


def test_every_recent_changes_section_points_at_fragments() -> None:
    """Each AGENT.md with a ``## Recent changes`` section must carry
    the fragment pointer, so the next component added cannot quietly
    reopen the prepend conflict factory."""
    offenders = []
    checked = 0
    for agent_md in _repo_files("AGENT.md"):
        body = " ".join(agent_md.read_text().split())
        if "## Recent changes" not in body:
            continue
        checked += 1
        if POINTER_PHRASE not in body:
            offenders.append(str(agent_md.relative_to(REPO_ROOT)))
    assert checked >= 14, (
        f"only {checked} Recent-changes AGENT.md files found — the repo "
        "walk is broken (14 existed when this pin was written)"
    )
    assert not offenders, (
        "AGENT.md files with a Recent-changes section but no fragment "
        f"pointer: {offenders} — add the pointer block per "
        "docs/agent-md-schema.md instead of prepending entries"
    )


def test_agent_changes_fragments_are_well_formed() -> None:
    """Every file in any ``agent-changes/`` directory must match
    ``YYYY-MM-DD-<task-short-or-pr-number>[-slug].md`` so newest-first
    ordering falls out of the filename sort, attribution stays
    greppable, and the allocator-unique prefix makes same-day
    same-component path collisions impossible."""
    bad = []
    fragments = []
    for d in _repo_files("agent-changes"):
        if not d.is_dir():
            continue
        for f in d.iterdir():
            fragments.append(f)
            if not FRAGMENT_NAME.match(f.name):
                bad.append(str(f.relative_to(REPO_ROOT)))
    # The convention dogfoods itself: the fragment describing this very
    # change exists and is the floor for the walk being alive.
    assert any(
        f.name == "2026-06-12-986c5cf6-recent-changes-fragments.md"
        for f in fragments
    ), "the convention's own dogfood fragment is missing"
    assert not bad, (
        f"malformed agent-changes fragment names: {bad} — use "
        "YYYY-MM-DD-<task-short-or-pr-number>[-slug].md (prefix must be "
        "the 8-hex task-id short form or the PR number)"
    )


def test_schema_doc_defines_the_fragment_flow() -> None:
    """The authoring flow lives in the schema doc the templates point
    writers at — losing it orphans the per-AGENT.md pointers."""
    schema = " ".join(
        (REPO_ROOT / "docs" / "agent-md-schema.md").read_text().split()
    )
    assert "agent-changes/YYYY-MM-DD-<task-or-pr-slug>.md" in schema
    assert "never in-file prepends" in schema
