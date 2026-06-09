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
# ``down_revision`` accepts three concrete forms in real migrations:
#   1. ``"abc123"``                  — single parent (linear chain).
#   2. ``None``                       — root migration.
#   3. ``("abc", "def")`` / ``["abc", "def"]`` — merge migration
#      consuming N parents (alembic's multi-head reconciliation
#      surface, ``alembic merge`` output). Recognizing this third form
#      is load-bearing: a merge migration is the SOLUTION to a
#      multi-head condition, not the cause of one. Treating the merge
#      as a child of each parent is what un-flags both parents as
#      heads. Catching this is exactly the false positive that bit
#      the linter against the live treadmill repo
#      (``20260605_1615_merge_architect_gold_and_dspy_variant_heads.py``).
_DOWN_REVISION_LINE_RE = re.compile(
    r"^down_revision(?:\s*:\s*[^=]+)?\s*=\s*(.+?)(?:\s*#.*)?$",
    re.MULTILINE,
)
_QUOTED_ID_RE = re.compile(r"['\"]([^'\"]+)['\"]")


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


def _parse_migration_file(
    path: Path,
) -> tuple[str, tuple[str, ...]] | None:
    """Extract ``(revision, parents)`` from a migration file.

    ``parents`` is a tuple of zero or more revision ids: empty for a
    root migration (``down_revision = None``), one element for a
    linear migration, multiple for a merge migration whose
    ``down_revision`` is a tuple / list literal.

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

    down_match = _DOWN_REVISION_LINE_RE.search(text)
    if down_match is None:
        # File declares a revision but no down_revision — treat as
        # root (Alembic's convention for the initial migration is
        # ``down_revision = None``).
        return revision, ()

    rhs = down_match.group(1).strip()
    # ``None`` literal — root migration.
    if rhs == "None":
        return revision, ()
    # ``"abc"`` / ``'abc'`` — linear chain.
    # ``("abc", "def")`` / ``["abc", "def"]`` — merge migration. The
    # quoted-id regex extracts every quoted token from the RHS; tuple
    # / list literal vs single string is transparent here, which is
    # the property we want.
    parents = tuple(_QUOTED_ID_RE.findall(rhs))
    return revision, parents


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
    # collisions surface with both branches named. A child of N
    # parents (alembic merge migration) registers under each parent.
    # ``None`` parent key represents a root migration; multiple roots
    # are legitimate and skipped by the multi-head check.
    parent_to_children: dict[str | None, list[tuple[str, Path]]] = {}

    for path in sorted(versions_dir.iterdir()):
        if not path.is_file() or path.suffix != ".py":
            continue
        if path.name == "__init__.py":
            continue
        parsed = _parse_migration_file(path)
        if parsed is None:
            continue
        revision, parents = parsed
        rev_to_files.setdefault(revision, []).append(path)
        if not parents:
            # Root migration — no parent edge to register, but record
            # the implicit ``None`` key so root-count diagnostics stay
            # available downstream.
            parent_to_children.setdefault(None, []).append((revision, path))
            continue
        for parent in parents:
            parent_to_children.setdefault(parent, []).append((revision, path))

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

    # Multi-head: more than one terminal revision exists.
    #
    # A "head" is a revision that no other migration claims as a
    # parent — the tip of the chain. Alembic's upgrade walks each
    # head independently, so > 1 head means the chain branched and
    # never reconverged. Two migrations sharing a ``down_revision``
    # is NOT inherently a violation: when a later merge migration
    # consumes both branches (alembic's ``down_revision = (rev_a,
    # rev_b)`` tuple shape, e.g. ``alembic merge`` output), the
    # chain re-converges to a single head and the multi-head
    # condition is resolved. Counting terminal heads is the
    # structurally correct check; counting shared parents would
    # false-positive every legitimate merge migration. Caught the
    # hard way by tonight's run against the live treadmill repo:
    # ``20260605_1615_merge_architect_gold_and_dspy_variant_heads.py``
    # is the merge that reconverges the ``20260604_0200`` /
    # ``20260604_1200`` branches.
    declared_revisions = set(rev_to_files.keys())
    heads = sorted(
        rev for rev in declared_revisions if rev not in parent_to_children
    )
    if len(heads) > 1:
        head_files = tuple(rev_to_files[h][0] for h in heads)
        violations.append(
            ChainViolation(
                kind="multi-head",
                revisions=tuple(heads),
                files=head_files,
                detail=(
                    f"chain has {len(heads)} terminal heads "
                    f"{heads!r}; alembic upgrade would walk each "
                    "independently, leaving the schema in a "
                    "branch-dependent state. Pick the head you want "
                    "to keep, rebase the others' down_revision to "
                    "point at the keeper (linearize), OR add a merge "
                    "migration with ``down_revision = "
                    f"({heads!r})`` to reconverge the branches. "
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
