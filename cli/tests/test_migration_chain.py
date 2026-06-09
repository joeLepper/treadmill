"""Tests for the Alembic migration-chain linter (post-mortem surprise C)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from treadmill_cli.cli import app
from treadmill_cli.migration_chain import (
    ChainViolation,
    find_chain_violations,
)


# ── Migration-file factory ──────────────────────────────────────────────


_MIGRATION_TEMPLATE = '''"""{title}.

Revision ID: {revision}
"""

from typing import Sequence, Union

from alembic import op


revision: str = "{revision}"
down_revision: Union[str, None] = {down}
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
'''


def _write_migration(
    versions_dir: Path,
    *,
    revision: str,
    down_revision: str | None,
    title: str = "test migration",
    filename: str | None = None,
) -> Path:
    """Write a synthetic migration file under ``versions_dir``."""
    versions_dir.mkdir(parents=True, exist_ok=True)
    if down_revision is None:
        down = "None"
    else:
        down = f'"{down_revision}"'
    name = filename or f"{revision}_{title.replace(' ', '_')}.py"
    path = versions_dir / name
    path.write_text(
        _MIGRATION_TEMPLATE.format(
            title=title, revision=revision, down=down
        ),
        encoding="utf-8",
    )
    return path


# ── Pure-logic tests ────────────────────────────────────────────────────


class TestCleanChain:
    def test_single_root_migration(self, tmp_path: Path) -> None:
        _write_migration(tmp_path, revision="rev_a", down_revision=None)
        assert find_chain_violations(tmp_path) == []

    def test_linear_chain_of_three(self, tmp_path: Path) -> None:
        _write_migration(tmp_path, revision="rev_a", down_revision=None)
        _write_migration(tmp_path, revision="rev_b", down_revision="rev_a")
        _write_migration(tmp_path, revision="rev_c", down_revision="rev_b")
        assert find_chain_violations(tmp_path) == []

    def test_init_py_is_ignored(self, tmp_path: Path) -> None:
        _write_migration(tmp_path, revision="rev_a", down_revision=None)
        (tmp_path / "__init__.py").write_text("", encoding="utf-8")
        assert find_chain_violations(tmp_path) == []


class TestMultiHeadCollision:
    """The actual failure mode from the ADR-0085+0086 post-mortem."""

    def test_two_migrations_share_parent_branches_chain(
        self, tmp_path: Path
    ) -> None:
        _write_migration(tmp_path, revision="parent", down_revision=None)
        _write_migration(tmp_path, revision="bert_branch", down_revision="parent")
        _write_migration(tmp_path, revision="carla_branch", down_revision="parent")

        violations = find_chain_violations(tmp_path)
        assert len(violations) == 1
        v = violations[0]
        assert v.kind == "multi-head"
        assert set(v.revisions) == {"bert_branch", "carla_branch"}
        assert "down_revision='parent'" in v.detail

    def test_three_migrations_share_parent(self, tmp_path: Path) -> None:
        _write_migration(tmp_path, revision="parent", down_revision=None)
        for child in ("a", "b", "c"):
            _write_migration(tmp_path, revision=child, down_revision="parent")
        violations = find_chain_violations(tmp_path)
        assert len(violations) == 1
        assert set(violations[0].revisions) == {"a", "b", "c"}


class TestDuplicateRevision:
    def test_two_files_declare_same_revision(self, tmp_path: Path) -> None:
        _write_migration(
            tmp_path,
            revision="rev_a",
            down_revision=None,
            filename="20260609_aaa.py",
        )
        _write_migration(
            tmp_path,
            revision="rev_a",
            down_revision=None,
            filename="20260609_bbb.py",
        )
        violations = find_chain_violations(tmp_path)
        kinds = {v.kind for v in violations}
        assert "duplicate-revision" in kinds
        # And the duplicate also produces a multi-head false positive
        # against itself (both files declare the same root and that's
        # one head — None root is excluded). Confirm we don't flag it.
        assert "multi-head" not in kinds


class TestDanglingDownRevision:
    def test_down_revision_points_to_nonexistent_revision(
        self, tmp_path: Path
    ) -> None:
        _write_migration(
            tmp_path, revision="rev_a", down_revision="nonexistent"
        )
        violations = find_chain_violations(tmp_path)
        assert len(violations) == 1
        v = violations[0]
        assert v.kind == "dangling-down-revision"
        assert v.revisions == ("rev_a",)
        assert "nonexistent" in v.detail


class TestErrorPaths:
    def test_missing_versions_dir_raises_filenotfound(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(FileNotFoundError):
            find_chain_violations(tmp_path / "does-not-exist")

    def test_empty_directory_returns_empty_list(self, tmp_path: Path) -> None:
        assert find_chain_violations(tmp_path) == []

    def test_non_python_file_is_ignored(self, tmp_path: Path) -> None:
        _write_migration(tmp_path, revision="rev_a", down_revision=None)
        (tmp_path / "README.md").write_text("docs", encoding="utf-8")
        assert find_chain_violations(tmp_path) == []


# ── Regex robustness ────────────────────────────────────────────────────


class TestRegexAcceptsAnnotationVariants:
    """Real Alembic migrations come in two annotation flavors over time:
    ``revision: str = "..."`` and ``revision = "..."`` (untyped). The
    parser must accept both."""

    def test_untyped_revision_assignment(self, tmp_path: Path) -> None:
        (tmp_path / "rev_x.py").write_text(
            'revision = "rev_x"\ndown_revision = None\n',
            encoding="utf-8",
        )
        assert find_chain_violations(tmp_path) == []

    def test_typed_revision_assignment(self, tmp_path: Path) -> None:
        (tmp_path / "rev_y.py").write_text(
            'revision: str = "rev_y"\ndown_revision: str | None = None\n',
            encoding="utf-8",
        )
        assert find_chain_violations(tmp_path) == []


# ── CLI-wrapper tests ───────────────────────────────────────────────────


class TestCli:
    def test_clean_chain_exits_zero(self, tmp_path: Path) -> None:
        _write_migration(tmp_path, revision="rev_a", down_revision=None)
        runner = CliRunner()
        result = runner.invoke(
            app, ["plan", "check-migration-chain", "--versions-dir", str(tmp_path)]
        )
        assert result.exit_code == 0, result.output
        assert "chain clean" in result.output

    def test_multihead_exits_one(self, tmp_path: Path) -> None:
        _write_migration(tmp_path, revision="parent", down_revision=None)
        _write_migration(tmp_path, revision="bert", down_revision="parent")
        _write_migration(tmp_path, revision="carla", down_revision="parent")
        runner = CliRunner()
        result = runner.invoke(
            app, ["plan", "check-migration-chain", "--versions-dir", str(tmp_path)]
        )
        assert result.exit_code == 1
        assert "multi-head" in result.output

    def test_missing_dir_exits_two(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "plan",
                "check-migration-chain",
                "--versions-dir",
                str(tmp_path / "no-such-dir"),
            ],
        )
        assert result.exit_code == 2
