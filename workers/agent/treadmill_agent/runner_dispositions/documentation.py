"""``documentation`` disposition — amend doc artifacts + Class C escalation.

Per ADR-0032 §wf-doc-amend, this handler:

  1. Stages the amended artifact(s) produced by role-documentarian.
  2. Empty diff → ``CodeAuthorError`` (the role was asked to amend a doc
     and produced nothing).
  3. Parses Claude's summary for a JSON gap envelope containing
     ``gap_class``.  A ``gap_class`` of ``"C"`` triggers escalation:
       a. writes ``docs/learnings/<date>-<slug>-gap.md`` into the repo so
          the file is committed alongside the amended artifact;
       b. includes ``escalate`` in the ``StepOutput`` payload so the
          coordination consumer dispatches ``wf-architecture-resolve``
          against the same task.
  4. Commits → pushes → opens/updates PR (idempotent per PR #120's
     ``open_pr`` check for existing PRs on the branch).

Gap-class detection follows the same JSON-envelope pattern as ADR-0027's
``review`` disposition: the last ````json ... `````` block in the summary
whose parsed object contains ``"gap_class"`` wins.  Class A / B summaries
carry no such block (or a block without ``gap_class``), so they flow
straight through as a clean push with ``decision="pushed"``.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any

from treadmill_agent import git
from treadmill_agent.events import Artifact, Metadata, StepOutput
from treadmill_agent.runner_dispositions._context import DispositionContext

logger = logging.getLogger("treadmill.agent.documentation")

_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_SLUG_UNSAFE_RE = re.compile(r"[^a-z0-9]+")


def _extract_gap_envelope(summary: str) -> dict[str, Any] | None:
    """Return the last JSON block that contains ``gap_class``, or None."""
    envelope: dict[str, Any] | None = None
    for m in _JSON_BLOCK_RE.finditer(summary):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if "gap_class" in data:
            envelope = data
    return envelope


def _safe_slug(raw: str) -> str:
    lowered = raw.lower()
    cleaned = _SLUG_UNSAFE_RE.sub("-", lowered).strip("-")
    return cleaned or "gap"


def _write_learning(
    repo_dir: Path,
    *,
    slug: str,
    gap_class: str,
    gap_summary: str,
    task_id: str,
) -> Path:
    """Write ``docs/learnings/<date>-<slug>-gap.md`` and return its path.

    The file is written into the working tree; ``commit_all`` (called by
    the handler immediately after) stages and commits it together with
    the amended doc artifact.
    """
    today = date.today().isoformat()
    learning_dir = repo_dir / "docs" / "learnings"
    learning_dir.mkdir(parents=True, exist_ok=True)
    path = learning_dir / f"{today}-{slug}-gap.md"
    path.write_text(
        f"# Gap: {slug}\n\n"
        f"**Class:** {gap_class}  \n"
        f"**Task:** {task_id}  \n"
        f"**Date:** {today}\n\n"
        f"{gap_summary}\n"
    )
    return path


def handle(ctx: DispositionContext) -> StepOutput:
    """Stage amendments, detect gap class, commit, push, open PR."""
    from treadmill_agent import claude_code
    from treadmill_agent.runner import _commit_message  # local import avoids cycle

    git.stage_all(ctx.repo_dir)
    if not ctx.is_dry_run and not git.has_staged_changes(ctx.repo_dir):
        raise claude_code.CodeAuthorError(
            "documentation handler: Claude Code produced no changes to commit"
        )

    summary = ctx.claude_result.summary or ""
    gap_envelope = _extract_gap_envelope(summary)
    is_class_c = gap_envelope is not None and gap_envelope.get("gap_class") == "C"

    if is_class_c:
        raw_slug = gap_envelope.get("gap_slug", "gap")
        slug = _safe_slug(str(raw_slug))
        gap_summary_text = gap_envelope.get("gap_summary", "")
        learning_path = _write_learning(
            ctx.repo_dir,
            slug=slug,
            gap_class="C",
            gap_summary=gap_summary_text,
            task_id=ctx.ctx.task_id,
        )
        logger.info(
            "Class C gap detected; wrote learning at %s",
            learning_path.relative_to(ctx.repo_dir),
        )

    commit_sha = git.commit_all(ctx.repo_dir, _commit_message(ctx.ctx))
    git.push_branch(ctx.repo_dir, ctx.branch)
    pr_number, pr_url = git.open_pr(
        repo_dir=ctx.repo_dir,
        branch=ctx.branch,
        title=ctx.ctx.title,
        body=summary or ctx.ctx.title,
        repo=ctx.ctx.repo,
        mode=ctx.settings.repo_mode,
    )

    artifacts: list[Artifact] = [Artifact(kind="branch", value=ctx.branch)]
    if pr_url:
        artifacts.append(Artifact(kind="pr_url", value=pr_url))

    payload: dict[str, Any] = {}
    if pr_number is not None:
        payload["pr_number"] = pr_number
    if is_class_c:
        payload["escalate"] = {
            "workflow_id": "wf-architecture-resolve",
            "task_id": ctx.ctx.task_id,
            "gap_slug": slug,
        }

    return StepOutput(
        summary=summary,
        decision="pushed",
        commit_sha=commit_sha,
        artifacts=artifacts,
        payload=payload,
        metadata=Metadata(),
    )
