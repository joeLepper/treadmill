"""Deterministic PR-kind derivation from diff paths."""


def derive_kinds(diff_paths: list[str]) -> set[str]:
    """Derive PR kinds from a list of changed file paths.

    Returns a subset of {code, docs-only, test-only, infra, migration}.

    Algorithm:
    - migration: if any path matches alembic/versions/
    - test-only: if all changed paths are under tests/
    - docs-only: if all changed paths are under docs/ or end in .md
    - infra: if any path matches infra/ or Dockerfile
    - code: default catch-all

    For mixed PRs, code always wins as a tiebreaker. docs-only requires
    every changed path to qualify.

    Args:
        diff_paths: List of file paths that changed in the diff.

    Returns:
        A set of PR kind strings from {code, docs-only, test-only, infra, migration}.
    """
    if not diff_paths:
        return set()

    # Check if all paths are test-only
    if all(path.startswith("tests/") for path in diff_paths):
        return {"test-only"}

    # Check if all paths are docs-only
    if all(path.startswith("docs/") or path.endswith(".md") for path in diff_paths):
        return {"docs-only"}

    kinds = set()

    # Check for migration (any path)
    if any(path.startswith("alembic/versions/") for path in diff_paths):
        kinds.add("migration")

    # Check for infra (any path)
    if any(path.startswith("infra/") or path == "Dockerfile" for path in diff_paths):
        kinds.add("infra")

    # Code is the default catch-all; always included for mixed PRs
    kinds.add("code")

    return kinds
