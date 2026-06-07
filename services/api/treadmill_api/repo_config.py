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

from treadmill_api.models.onboarding import WorkerDeps

VALID_MODES = frozenset({"conform", "adapt"})


@dataclass(frozen=True)
class RepoConfig:
    repo: str
    mode: str = "conform"
    auto_merge_blocked: bool = False
    test_command: str | None = None
    lint_command: str | None = None
    # Per-repo Claude account name (ADR-0055). ``None`` => deployment default.
    # The named account must exist in ``Settings.claude_accounts``; resolution
    # happens at the worker's per-step credential fetch, not at onboarding.
    claude_account: str | None = None
    # Fallback Claude account when the primary hits a usage limit (ADR-0066).
    # ``None`` means no fallback; the worker stays on ``claude_account``.
    claude_account_fallback: str | None = None
    # Per-repo git author name override (ADR-0076). ``None`` defers to the
    # deployment default ``treadmill-agent``. Must be paired with
    # ``git_author_email`` (both None or both not None).
    git_author_name: str | None = None
    # Per-repo git author email override (ADR-0076). ``None`` defers to the
    # deployment default ``agent@treadmill``. Must be paired with
    # ``git_author_name`` (both None or both not None).
    git_author_email: str | None = None
    # Per-repo commit trailer override (ADR-0076). Three-valued: ``None``
    # uses the default trailer, empty string ``""`` suppresses it, any other
    # value is used verbatim.
    commit_trailer: str | None = None
    # Per-repo worker extras (ADR-0059). ``None`` is the wire shorthand for
    # "no extras"; ``OnboardingStore.get_repo_config`` always materializes it
    # as a non-None ``WorkerDeps`` (possibly with empty lists).
    worker_deps: WorkerDeps | None = None
    # Marks a repo as publicly visible on GitHub (ADR-0078). Drives the
    # secret-leak gate on vault writes — for public repos the gate
    # refuses content matching known-sensitive strings, for private
    # repos it is a no-op. Default ``False`` so existing rows are
    # behavior-neutral.
    is_public: bool = False
    # Per-repo extra sensitive-string blocklist (ADR-0078). The gate
    # always checks the hardcoded baseline (medicoder slug variants +
    # the public-repo account-id) plus any substrings declared here.
    # ``None`` means "baseline only". Stored as JSONB list of strings.
    sensitive_strings: list[str] | None = None
    # Per-repo worker hint channel enable (ADR-0081). Controls whether the
    # worker's request_hint tool is registered and operator_note is injected.
    # Defaults to true; flip to false to disable the hint channel.
    worker_hints_enabled: bool = True


def parse_repo_config(data: dict[str, Any]) -> RepoConfig:
    repo = data.get("repo")
    if not repo:
        raise ValueError("repo_config requires a non-empty 'repo' field")

    mode = data.get("mode", "conform")
    if mode not in VALID_MODES:
        raise ValueError(
            f"repo_config 'mode' must be one of {sorted(VALID_MODES)}; got {mode!r}"
        )

    worker_deps_data = data.get("worker_deps")
    worker_deps = (
        WorkerDeps.model_validate(worker_deps_data)
        if worker_deps_data is not None
        else None
    )

    sensitive_strings_data = data.get("sensitive_strings")
    if sensitive_strings_data is not None and not isinstance(
        sensitive_strings_data, list
    ):
        raise ValueError(
            "repo_config 'sensitive_strings' must be a list of strings or null"
        )
    if sensitive_strings_data is not None:
        if not all(isinstance(s, str) for s in sensitive_strings_data):
            raise ValueError(
                "repo_config 'sensitive_strings' entries must all be strings"
            )

    return RepoConfig(
        repo=repo,
        mode=mode,
        auto_merge_blocked=bool(data.get("auto_merge_blocked", False)),
        test_command=data.get("test_command"),
        lint_command=data.get("lint_command"),
        claude_account=data.get("claude_account"),
        claude_account_fallback=data.get("claude_account_fallback"),
        git_author_name=data.get("git_author_name"),
        git_author_email=data.get("git_author_email"),
        commit_trailer=data.get("commit_trailer"),
        worker_deps=worker_deps,
        is_public=bool(data.get("is_public", False)),
        sensitive_strings=sensitive_strings_data,
        worker_hints_enabled=bool(data.get("worker_hints_enabled", True)),
    )


def to_dict(config: RepoConfig) -> dict[str, Any]:
    return {
        "repo": config.repo,
        "mode": config.mode,
        "auto_merge_blocked": config.auto_merge_blocked,
        "test_command": config.test_command,
        "lint_command": config.lint_command,
        "claude_account": config.claude_account,
        "claude_account_fallback": config.claude_account_fallback,
        "git_author_name": config.git_author_name,
        "git_author_email": config.git_author_email,
        "commit_trailer": config.commit_trailer,
        "worker_deps": (
            config.worker_deps.model_dump()
            if config.worker_deps is not None
            else None
        ),
        "is_public": config.is_public,
        "sensitive_strings": config.sensitive_strings,
        "worker_hints_enabled": config.worker_hints_enabled,
    }
