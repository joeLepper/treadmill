"""Tests for PR kind derivation from diff paths."""

import pytest

from treadmill_agent.pr_kinds import derive_kinds


class TestTestOnlyPRs:
    """PRs that only change test files."""

    def test_single_test_file(self):
        assert derive_kinds(["tests/test_foo.py"]) == {"test-only"}

    def test_multiple_test_files(self):
        assert derive_kinds(["tests/test_foo.py", "tests/test_bar.py"]) == {"test-only"}

    def test_test_files_in_subdirectories(self):
        assert derive_kinds(
            ["tests/unit/test_foo.py", "tests/integration/test_bar.py"]
        ) == {"test-only"}

    def test_mixed_test_files_and_fixtures(self):
        assert derive_kinds(["tests/test_foo.py", "tests/fixtures/data.json"]) == {
            "test-only"
        }


class TestDocsOnlyPRs:
    """PRs that only change documentation."""

    def test_single_markdown_file(self):
        assert derive_kinds(["README.md"]) == {"docs-only"}

    def test_multiple_markdown_files(self):
        assert derive_kinds(["README.md", "CONTRIBUTING.md"]) == {"docs-only"}

    def test_files_in_docs_directory(self):
        assert derive_kinds(["docs/api.md", "docs/installation.md"]) == {"docs-only"}

    def test_markdown_files_mixed_with_docs_directory(self):
        assert derive_kinds(["docs/api.md", "CHANGELOG.md"]) == {"docs-only"}

    def test_nested_docs_directory(self):
        assert derive_kinds(["docs/guides/getting-started.md"]) == {"docs-only"}

    def test_docs_with_various_extensions_not_markdown(self):
        # Only .md files outside docs/ are considered docs-only.
        # Files in docs/ directory are always docs.
        assert derive_kinds(["docs/schema.json"]) == {"docs-only"}


class TestMigrationPRs:
    """PRs with database migrations."""

    def test_single_migration(self):
        assert derive_kinds(["alembic/versions/001_initial.py"]) == {
            "code",
            "migration",
        }

    def test_multiple_migrations(self):
        assert derive_kinds(
            ["alembic/versions/001_initial.py", "alembic/versions/002_add_column.py"]
        ) == {"code", "migration"}

    def test_migration_with_code_changes(self):
        assert derive_kinds(
            ["alembic/versions/001_initial.py", "src/app.py"]
        ) == {"code", "migration"}


class TestInfraPRs:
    """PRs with infrastructure changes."""

    def test_single_infra_file(self):
        assert derive_kinds(["infra/vpc.tf"]) == {"code", "infra"}

    def test_multiple_infra_files(self):
        assert derive_kinds(["infra/vpc.tf", "infra/security_group.tf"]) == {
            "code",
            "infra",
        }

    def test_nested_infra_directory(self):
        assert derive_kinds(["infra/kubernetes/deployment.yaml"]) == {
            "code",
            "infra",
        }

    def test_dockerfile_change(self):
        assert derive_kinds(["Dockerfile"]) == {"code", "infra"}

    def test_infra_with_code_changes(self):
        assert derive_kinds(["infra/vpc.tf", "src/app.py"]) == {
            "code",
            "infra",
        }


class TestCodeOnlyPRs:
    """PRs that only change code (non-test, non-docs)."""

    def test_single_code_file(self):
        assert derive_kinds(["src/app.py"]) == {"code"}

    def test_multiple_code_files(self):
        assert derive_kinds(["src/app.py", "src/utils.py"]) == {"code"}

    def test_code_in_subdirectories(self):
        assert derive_kinds(["src/handlers/api.py", "src/models/user.py"]) == {"code"}

    def test_various_code_file_extensions(self):
        assert derive_kinds(["src/app.py", "src/schema.json", "src/config.yaml"]) == {
            "code"
        }


class TestMixedPRs:
    """PRs that span multiple categories."""

    def test_code_plus_migration(self):
        assert derive_kinds(
            ["alembic/versions/001_initial.py", "src/models/user.py"]
        ) == {"code", "migration"}

    def test_code_plus_infra(self):
        assert derive_kinds(["infra/vpc.tf", "src/app.py"]) == {"code", "infra"}

    def test_code_plus_migration_plus_infra(self):
        assert derive_kinds(
            [
                "alembic/versions/001_initial.py",
                "infra/vpc.tf",
                "src/app.py",
            ]
        ) == {"code", "migration", "infra"}

    def test_code_plus_test_plus_docs(self):
        # Code wins the tiebreaker
        assert derive_kinds(
            ["src/app.py", "tests/test_app.py", "README.md"]
        ) == {"code"}

    def test_migration_plus_infra(self):
        assert derive_kinds(
            ["alembic/versions/001_initial.py", "infra/vpc.tf"]
        ) == {"code", "migration", "infra"}


class TestEdgeCases:
    """Edge cases and corner scenarios."""

    def test_empty_diff(self):
        assert derive_kinds([]) == set()

    def test_single_file_various_paths(self):
        # Ensure paths are matched from the beginning
        assert derive_kinds(["my_tests/test.py"]) == {"code"}
        assert derive_kinds(["tests_backup/test.py"]) == {"code"}

    def test_dockerfile_in_subdirectory(self):
        # Dockerfile must match exactly, not as a substring
        assert derive_kinds(["docker/Dockerfile"]) == {"code"}

    def test_alembic_directory_but_not_versions(self):
        # Only alembic/versions/ matches
        assert derive_kinds(["alembic/env.py"]) == {"code"}

    def test_docs_directory_with_non_markdown(self):
        # Files in docs/ are always considered docs
        assert derive_kinds(["docs/config.json"]) == {"docs-only"}

    def test_markdown_file_in_root(self):
        assert derive_kinds(["CHANGELOG.md"]) == {"docs-only"}

    def test_markdown_file_in_arbitrary_directory(self):
        # .md files outside of docs/ are still considered docs-only
        assert derive_kinds(["guides/GUIDE.md"]) == {"docs-only"}

    def test_infra_prefix_must_match_exactly(self):
        # Only paths starting with infra/
        assert derive_kinds(["infrastructure/main.tf"]) == {"code"}

    def test_complex_migration_path(self):
        assert derive_kinds(
            ["alembic/versions/20240515_001_add_user_table.py"]
        ) == {"code", "migration"}
