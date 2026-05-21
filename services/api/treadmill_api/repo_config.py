"""Per-repo onboarding config shape (ADR-0050, decision 5).

Holds the source-of-truth config that travels with each onboarded repo:
the chosen onboarding mode (``conform`` or ``adapt``), the discovered
build/test/lint commands, and an auto-merge safety valve that can block
auto-merge for the repo independently of any plan-level flag.

This module is the *shape* only — no router, no persistence, no DB
model. Persistence is a deliberate follow-up; for now callers parse/emit
plain dicts (e.g. parsed YAML/JSON) via :func:`parse_repo_config` and
:func:`to_dict`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

VALID_MODES = frozenset({"conform", "adapt"})


@dataclass(frozen=True)
class RepoConfig:
    repo: str
    mode: str = "conform"
    auto_merge_blocked: bool = False
    test_command: str | None = None
    lint_command: str | None = None


def parse_repo_config(data: dict[str, Any]) -> RepoConfig:
    repo = data.get("repo")
    if not repo:
        raise ValueError("repo_config requires a non-empty 'repo' field")

    mode = data.get("mode", "conform")
    if mode not in VALID_MODES:
        raise ValueError(
            f"repo_config 'mode' must be one of {sorted(VALID_MODES)}; got {mode!r}"
        )

    return RepoConfig(
        repo=repo,
        mode=mode,
        auto_merge_blocked=bool(data.get("auto_merge_blocked", False)),
        test_command=data.get("test_command"),
        lint_command=data.get("lint_command"),
    )


def to_dict(config: RepoConfig) -> dict[str, Any]:
    return {
        "repo": config.repo,
        "mode": config.mode,
        "auto_merge_blocked": config.auto_merge_blocked,
        "test_command": config.test_command,
        "lint_command": config.lint_command,
    }
