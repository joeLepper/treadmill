"""Alembic migration-chain linter — catches branch collisions before merge.

Post-mortem surprise C from the ADR-0085+0086 plan: when Bert (Task A,
``20260609_0900``) and Carla (Task C, ``20260609_1000``) authored
parallel migrations during the same dispatch window, both set
``down_revision = "20260608_2200"``. The chain branched. The first PR
to merge invalidated the other; the second needed a manual
``down_revision`` fix before it could land. Two sibling tasks bit on
this in one night.

The structural failure mode is **multi-head**: two migrations in the
chain claim the same parent. The Alembic CLI catches this at upgrade
time, but only against a live DB — too late, and not what a worker
sandbox can exercise. This module re-derives the same chain-graph
invariants purely from the files on disk, so any worker / pre-commit
hook / CI step can run the check with no DB.

The check exists as a pre-submit lint, not as a runtime guarantee — the
real correctness rests on Alembic at deploy time. This module's job is
to surface the collision at PR review time so the second author can
update ``down_revision`` before merge instead of after.

Public surface
==============

* :class:`ChainViolation` — typed record of one problem.
* :func:`find_chain_violations(versions_dir)` — directory in, list of
  violations out. Empty list = clean chain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


# Regex strict enough to avoid false positives on docstrings + comments
# but lax enough to handle both quote styles + the ``Union[str, None]``
# / ``str | None`` annotation variants that show up across Alembic
# templates over time.
_REVISION_RE = re.compile(
    r"^revision(?:\s*:\s*[^=]+)?\s*=\s*['\"]([^'\"]+)['\"]",
    re.MULTILINE,
)
_DOWN_REVISION_RE = re.compile(
    r"^down_revision(?:\s*:\s*[^=]+)?\s*=\s*(?:['\"]([^'\"]+)['\"]|None)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ChainViolation:
    """A single problem found in the migration-chain graph.

    ``kind`` is a short code suitable for grep + tooling.
    ``revisions`` lists the migrations involved (e.g. both branches of
    a multi-head collision). ``files`` carries the same ordering of
    paths so the operator can find the source files immediately.
    ``detail`` is the human-readable message.
    """

    kind: str
    revisions: tuple[str, ...]
    files: tuple[Path, ...]
    detail: str


def _parse_migration_file(path: Path) -> tuple[str, str | None] | None:
    """Extract ``(revision, down_revision)`` from a migration file.

    Returns ``None`` when the file doesn't look like a migration
    (missing the ``revision`` assignment). Non-migration files in the
    versions directory are silently ignored — alembic packages
    sometimes drop ``__init__.py`` or ``.pyc`` stubs that aren't real
    migrations.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    rev_match = _REVISION_RE.search(text)
    if rev_match is None:
        return None
    revision = rev_match.group(1)
    down_match = _DOWN_REVISION_RE.search(text)
    down_revision: str | None
    if down_match is None:
        # File declares a revision but no down_revision — treat as
        # root (Alembic's convention for the initial migration is
        # ``down_revision = None``).
        down_revision = None
    else:
        # Group 1 is the quoted id; ``None`` literal yields no group 1
        # match. Both branches of the regex collapse here.
        down_revision = down_match.group(1)
    return revision, down_revision


def find_chain_violations(versions_dir: Path) -> list[ChainViolation]:
    """Walk ``versions_dir`` and report chain-graph problems.

    Catches the failure modes that bit the ADR-0085+0086 plan:

    * **multi-head** — two migrations name the same ``down_revision``,
      so the chain branches. After merge, only one tip can be the
      head; the other tree is orphaned.
    * **duplicate-revision** — two files claim the same ``revision``
      identifier. Alembic would silently pick one at scan time; the
      other becomes unreachable.
    * **dangling-down-revision** — a migration's ``down_revision``
      points at an id that no file declares. Usually a stale local
      checkout or an accidental rename.

    Returns an empty list when the chain is clean (exactly one head,
    every ``down_revision`` resolvable, no duplicate revision ids).

    Args:
        versions_dir: Path to the directory holding Alembic migration
            files (e.g. ``services/api/alembic/versions``). Non-
            migration files in the directory are silently ignored.

    Returns:
        List of :class:`ChainViolation` records, one per problem found.
        Tests for "no violations" use ``assert find_chain_violations(d) == []``.
    """
    if not versions_dir.is_dir():
        raise FileNotFoundError(
            f"alembic versions directory not found: {versions_dir}"
        )

    # rev_id -> list of files declaring that rev_id
    rev_to_files: dict[str, list[Path]] = {}
    # parent_rev_id -> list of (child_rev_id, child_file) so multi-head
    # collisions surface with both branches named.
    parent_to_children: dict[str | None, list[tuple[str, Path]]] = {}

    for path in sorted(versions_dir.iterdir()):
        if not path.is_file() or path.suffix != ".py":
            continue
        if path.name == "__init__.py":
            continue
        parsed = _parse_migration_file(path)
        if parsed is None:
            continue
        revision, down_revision = parsed
        rev_to_files.setdefault(revision, []).append(path)
        parent_to_children.setdefault(down_revision, []).append((revision, path))

    violations: list[ChainViolation] = []

    # Duplicate-revision: same id declared by two files.
    for revision, files in sorted(rev_to_files.items()):
        if len(files) <= 1:
            continue
        violations.append(
            ChainViolation(
                kind="duplicate-revision",
                revisions=(revision,) * len(files),
                files=tuple(files),
                detail=(
                    f"two or more migration files declare "
                    f"revision={revision!r}: "
                    f"{[str(p.name) for p in files]}. Alembic will "
                    "silently pick one at scan time and the other "
                    "becomes unreachable. Rename the duplicate."
                ),
            )
        )

    # Multi-head: two migrations point at the same down_revision.
    # Skip the ``None`` parent — multiple root migrations are
    # legitimate when an Alembic branch with branch_labels is in use,
    # which we don't do today but the linter should not false-positive on.
    for parent, children in sorted(
        parent_to_children.items(), key=lambda kv: (kv[0] is None, kv[0])
    ):
        if parent is None:
            continue
        if len(children) <= 1:
            continue
        child_ids = tuple(child[0] for child in children)
        child_files = tuple(child[1] for child in children)
        violations.append(
            ChainViolation(
                kind="multi-head",
                revisions=child_ids,
                files=child_files,
                detail=(
                    f"chain collision: migrations "
                    f"{[str(r) for r in child_ids]} all declare "
                    f"down_revision={parent!r}. Pick one to keep at "
                    "that parent; rebase the others' down_revision to "
                    "point at the eventual head of the merge order. "
                    "(Post-mortem surprise C from the ADR-0085+0086 plan — "
                    "the exact failure mode that bit Bert + Carla on "
                    "2026-06-09.)"
                ),
            )
        )

    # Dangling-down-revision: a parent_to_children key (other than
    # None) that isn't declared anywhere in rev_to_files.
    declared = set(rev_to_files.keys())
    for parent, children in sorted(
        parent_to_children.items(), key=lambda kv: (kv[0] is None, kv[0])
    ):
        if parent is None:
            continue
        if parent in declared:
            continue
        # Every child claims this missing parent — surface all of them.
        for child_id, child_file in children:
            violations.append(
                ChainViolation(
                    kind="dangling-down-revision",
                    revisions=(child_id,),
                    files=(child_file,),
                    detail=(
                        f"migration {child_id!r} declares "
                        f"down_revision={parent!r} but no file in "
                        f"{versions_dir.name}/ declares that revision. "
                        "Either the file was renamed/deleted, the local "
                        "checkout is stale, or the down_revision is a typo."
                    ),
                )
            )

    return violations
