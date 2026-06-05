"""Shared context passed to per-kind disposition handlers.

Per ADR-0022, the runner's ``_execute`` builds this struct once after
the shared prefix (clone, checkout, Claude Code) and hands it to the
handler picked by the dispatch table. Each handler reads what it
needs and ignores the rest — e.g. ``review`` reads ``pr_number`` and
``summary``, ``code`` reads ``branch`` / ``settings`` / ``repo_dir``.

Why a struct rather than positional args: ADR-0022 lists four
handlers today plus open slots for future kinds (the Ralph-loop
validation ADR will likely add one). A struct keeps the handler
signatures stable when a new kind needs another field.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from treadmill_agent.api_client import WorkerContext
from treadmill_agent.claude_code import CodeAuthorResult
from treadmill_agent.config import Settings
from treadmill_api.repo_config import RepoConfig


@dataclass(frozen=True)
class DispositionContext:
    """The fixed bundle a per-kind handler receives.

    ``ctx`` carries the full step context from the API (role,
    workflow, task metadata, prior steps, ``pr_number``).
    ``claude_result`` is the captured Claude Code output (``summary``
    is the stdout text). ``repo_dir`` is the freshly-cloned working
    tree; the handler may inspect ``git diff`` to verify constraints
    (e.g. plan_doc's docs/plans/ confinement) or stage + commit. The
    ``branch`` field is the branch name the runner computed from
    ``ctx.task_id`` + ``ctx.title``; only code-kind handlers use it.
    ``settings`` carries the worker's static config (repo mode, etc).
    ``is_dry_run`` is the runner's dry-run flag, surfaced so handlers
    can short-circuit the LLM-driven paths in tests.
    ``repo_config`` carries the per-repo onboarding config (ADR-0050)
    including git author + trailer overrides (ADR-0076); ``None`` when
    the repo was not onboarded or the fetch failed.
    """

    ctx: WorkerContext
    claude_result: CodeAuthorResult
    repo_dir: Path
    branch: str
    settings: Settings
    is_dry_run: bool
    repo_config: RepoConfig | None = None
