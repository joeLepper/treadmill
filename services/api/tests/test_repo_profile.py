"""Unit tests for the discovery repo-profile schema (ADR-0050)."""

from __future__ import annotations

from treadmill_api.repo_profile import (
    RepoProfile,
    from_dict,
    recommend_mode,
    to_dict,
)


def test_round_trip_populated_profile() -> None:
    p = RepoProfile(
        repo="o/r",
        languages=["python", "typescript"],
        build_command="make build",
        test_command="pytest",
        lint_command="ruff check",
        doc_paths=["README.md", "docs/arch.md"],
        components=["api", "worker"],
        ci="github-actions",
        has_agent_context=True,
    )
    assert from_dict(to_dict(p)) == p


def test_defaults() -> None:
    p = RepoProfile(repo="o/r")
    assert p.languages == []
    assert p.doc_paths == []
    assert p.components == []
    assert p.build_command is None
    assert p.test_command is None
    assert p.lint_command is None
    assert p.ci is None
    assert p.has_agent_context is False


def test_recommend_mode_adapt_when_has_agent_context() -> None:
    p = RepoProfile(repo="o/r", has_agent_context=True)
    assert recommend_mode(p) == "adapt"


def test_recommend_mode_adapt_when_three_or_more_doc_paths() -> None:
    p = RepoProfile(
        repo="o/r",
        doc_paths=["README.md", "docs/arch.md", "docs/onboarding.md"],
    )
    assert recommend_mode(p) == "adapt"


def test_recommend_mode_conform_for_sparse_profile() -> None:
    p = RepoProfile(
        repo="o/r",
        doc_paths=["README.md", "docs/arch.md"],
        has_agent_context=False,
    )
    assert recommend_mode(p) == "conform"
