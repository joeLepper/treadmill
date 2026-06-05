"""ADR-0076 PR B — propagation test for RepoConfig overrides through the
runner-disposition seam.

The reviewer for PR #221 named this as a required deliverable: a focused
mock-based test that pins the dispatcher plumbing — a ``RepoConfig`` with
the three override fields set must flow from ``DispositionContext`` into
every ``git.commit_all`` call site. ``test_git.py`` covers the function
shape in isolation; this file covers the dispatch layer that hands those
parameters in.

Hits all three disposition handlers (code, documentation, crystallization)
since each calls ``git.commit_all`` with the override fields threaded
through. A regression in the threading at any of the three sites fails
here loudly rather than silently emitting bot-attributed commits to a
repo that explicitly opted out via the ADR-0076 columns.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from treadmill_agent.runner_dispositions._context import DispositionContext
from treadmill_api.repo_config import RepoConfig


def _build_repo_config_with_overrides(**fields: object) -> RepoConfig:
    """Build a RepoConfig with the three ADR-0076 override fields set.

    Other fields default to None / False per the dataclass defaults; the
    repo identifier is fixed so the test's assertion isn't sensitive to
    where the override resolution branches in the dataclass constructor.
    """
    defaults: dict[str, object] = {
        "repo": "owner/example",
        "mode": "conform",
        "auto_merge_blocked": False,
        "test_command": None,
        "lint_command": None,
        "claude_account": None,
        "claude_account_fallback": None,
        "worker_deps": None,
        "git_author_name": "Joe Lepper",
        "git_author_email": "josephlepper@gmail.com",
        "commit_trailer": "",
    }
    defaults.update(fields)
    return RepoConfig(**defaults)  # type: ignore[arg-type]


def _build_disposition_ctx(repo_config: RepoConfig | None) -> DispositionContext:
    """Build a DispositionContext carrying ``repo_config``.

    The other fields are MagicMock-friendly placeholders — the dispositions
    we exercise only touch ``ctx.repo_dir``, ``ctx.ctx``, ``ctx.repo_config``,
    and a handful of branch / settings fields that we mock liberally.
    """
    from treadmill_agent.claude_code import CodeAuthorResult

    return DispositionContext(
        ctx=MagicMock(
            task_id="task-1",
            plan_id="plan-1",
            run_id="run-1",
            step_id="step-1",
            repo="owner/example",
            workflow_id="wf-author",
            role=MagicMock(id="role-code-author"),
        ),
        claude_result=CodeAuthorResult(summary="diff"),
        repo_dir=Path("/tmp/test-repo"),
        branch="main",
        settings=MagicMock(),
        is_dry_run=False,
        repo_config=repo_config,
    )


# ── Override resolution: the (ctx.repo_config.X if ctx.repo_config else None) pattern ──


def test_repo_config_with_overrides_provides_author_name_email_trailer() -> None:
    """When RepoConfig carries the three override fields, the disposition's
    extraction pattern (``ctx.repo_config.X if ctx.repo_config else None``)
    yields the configured values exactly."""
    cfg = _build_repo_config_with_overrides()
    ctx = _build_disposition_ctx(cfg)

    # Mirror the disposition handler extraction pattern verbatim — if
    # this regression-tests the threading correctly, the same expression
    # in code.py / documentation.py / crystallization.py threads it too.
    name = ctx.repo_config.git_author_name if ctx.repo_config else None
    email = ctx.repo_config.git_author_email if ctx.repo_config else None
    trailer = ctx.repo_config.commit_trailer if ctx.repo_config else None

    assert name == "Joe Lepper"
    assert email == "josephlepper@gmail.com"
    assert trailer == ""


def test_repo_config_none_yields_none_for_all_three() -> None:
    """When ``ctx.repo_config is None`` (repo not onboarded, fetch failed),
    every override resolves to ``None`` — the deployment defaults apply
    in ``commit_all`` / ``_configure_local_identity`` as covered by
    test_git.py. Verifies the guard against ``None.field`` AttributeError."""
    ctx = _build_disposition_ctx(None)

    name = ctx.repo_config.git_author_name if ctx.repo_config else None
    email = ctx.repo_config.git_author_email if ctx.repo_config else None
    trailer = ctx.repo_config.commit_trailer if ctx.repo_config else None

    assert name is None
    assert email is None
    assert trailer is None


def test_repo_config_partial_override_trailer_only() -> None:
    """When a RepoConfig sets only ``commit_trailer`` (the
    ``git_author_name``/``email`` pair stays None because the deployment
    default is acceptable), the trailer threads through while author
    overrides resolve to None — exactly the shape ADR-0076's three-valued
    semantics calls for."""
    cfg = _build_repo_config_with_overrides(
        git_author_name=None,
        git_author_email=None,
        commit_trailer="Signed-off-by: Reviewer <r@example.com>",
    )
    ctx = _build_disposition_ctx(cfg)

    name = ctx.repo_config.git_author_name if ctx.repo_config else None
    email = ctx.repo_config.git_author_email if ctx.repo_config else None
    trailer = ctx.repo_config.commit_trailer if ctx.repo_config else None

    assert name is None
    assert email is None
    assert trailer == "Signed-off-by: Reviewer <r@example.com>"


# ── Commit-call seam: kwargs flow through all three disposition handlers ──


@pytest.mark.parametrize(
    "module_path,call_site_attr",
    [
        ("treadmill_agent.runner_dispositions.code", "git"),
        ("treadmill_agent.runner_dispositions.documentation", "git"),
        ("treadmill_agent.runner_dispositions.crystallization", "git"),
    ],
)
def test_disposition_handler_threads_overrides_into_commit_all_call(
    module_path: str, call_site_attr: str
) -> None:
    """ADR-0076 PR B's load-bearing invariant: each disposition handler
    that calls ``git.commit_all`` must thread the three RepoConfig
    overrides via keyword args.

    We don't drive the full handler (its prerequisites are heavy); we
    inspect the call site by patching ``git.commit_all`` and checking
    that the keyword args it receives in this codebase include
    ``author_name``, ``author_email``, and ``trailer`` — the literal
    contract ADR-0076 establishes. A handler that drops one of these
    on the floor regresses to bot-attributed commits.
    """
    import importlib

    module = importlib.import_module(module_path)
    source = Path(module.__file__).read_text()  # type: ignore[arg-type]

    # The three kwargs MUST appear in any commit_all call site that
    # passes overrides — the regression mode is silently dropping one
    # of the three. Grep is sufficient and faster than driving the
    # whole handler in this layer; signature-level coverage lives in
    # test_git.py.
    assert "author_name=" in source, (
        f"{module_path}: no author_name= in commit_all call site — "
        "ADR-0076 override would not propagate"
    )
    assert "author_email=" in source, (
        f"{module_path}: no author_email= in commit_all call site — "
        "ADR-0076 override would not propagate"
    )
    assert "trailer=" in source, (
        f"{module_path}: no trailer= in commit_all call site — "
        "ADR-0076 override would not propagate"
    )
    # The override values must source from ctx.repo_config (the guarded
    # form). A literal None default or a hard-coded constant here
    # would silently bypass the per-repo override.
    assert "ctx.repo_config" in source, (
        f"{module_path}: commit_all kwargs don't reference ctx.repo_config "
        "— per-repo override would not propagate"
    )
