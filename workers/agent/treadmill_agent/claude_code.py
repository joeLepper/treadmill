"""Claude Code CLI wrapper.

The worker shells out to ``claude`` (Claude Code CLI) inside its
container with the role's ``system_prompt`` and ``model``. Auth comes
from the user's mounted ``~/.claude/.credentials.json`` — no API key
management at v0; users leverage their Claude subscription.

Phase 2 minimum: a single non-interactive invocation that reads the
prompt, makes file edits in the current working directory, and exits.
``--print`` (Claude Code's headless mode) streams output to stdout so
we can capture a summary.

A future ADR layers in skill + hook composition (RoleSkill / RoleHook
ordered lists already exist on the API side); v0 just bundles them into
the prompt as plain text.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from treadmill_agent.api_client import PriorStep, Role

logger = logging.getLogger("treadmill.agent.claude_code")


@dataclass(frozen=True)
class CodeAuthorResult:
    summary: str
    """A short text summary produced by Claude Code (its ``--print`` output).
    Stored on ``step.output.summary`` so the user can see what changed
    without diffing."""


class CodeAuthorError(RuntimeError):
    """Surface non-zero exit codes from the Claude Code CLI."""


def run_claude_code(
    *,
    repo_dir: Path,
    role: Role,
    task_title: str,
    task_description: str | None,
    plan_intent: str | None,
    prior_steps: list[PriorStep] | None = None,
    timeout_seconds: int = 600,
) -> CodeAuthorResult:
    """Drive Claude Code in ``repo_dir`` and return the captured summary.

    The prompt bundles plan intent + task title + description + role's
    system_prompt + skill content + (for multi-step workflows) prior
    step outputs. Claude Code makes file edits directly in ``repo_dir``
    because the worker invokes it with ``cwd=repo_dir``.

    ``prior_steps`` is the ordered list of completed prior steps in the
    same run (per ADR-0015's ``prior_steps`` API extension). For
    two-step workflows the action role consumes the analyzer's
    ``task_directive`` from ``prior_steps[-1].output.payload``; the
    prompt-composer folds this in automatically.
    """
    binary = _find_binary()
    prompt = _compose_prompt(
        role=role, task_title=task_title,
        task_description=task_description, plan_intent=plan_intent,
        prior_steps=prior_steps or [],
    )

    cmd = [
        binary, "--print",
        "--model", role.model,
        # ``acceptEdits`` is the permission mode that lets Claude Code's
        # Edit / Write tools land changes without a TTY prompt. Without
        # this, ``--print`` mode emits text like "Adding it now" but
        # silently drops the Edit call — the worker would then raise
        # ``CodeAuthorError("Claude Code produced no changes to commit")``
        # on every real-Claude run. Discovered while wiring B.11
        # (real-Claude opt-in smoke); see closure plan's running log.
        # Bash + non-edit tools still respect the role's broader
        # sandbox, which is enforced by the container boundary.
        "--permission-mode", "acceptEdits",
        "--append-system-prompt", role.system_prompt,
        prompt,
    ]
    logger.info("running claude code: model=%s cwd=%s", role.model, repo_dir)
    result = subprocess.run(
        cmd, cwd=str(repo_dir),
        capture_output=True, text=True,
        timeout=timeout_seconds,
    )
    if result.returncode != 0:
        raise CodeAuthorError(
            f"claude exited {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return CodeAuthorResult(summary=result.stdout.strip() or "(no summary)")


def _find_binary() -> str:
    """Return the resolved path to the ``claude`` binary, or raise.

    ``CLAUDE_BINARY`` env var overrides for tests / non-standard installs.
    """
    override = os.environ.get("CLAUDE_BINARY")
    if override:
        return override
    found = shutil.which("claude")
    if found is None:
        raise CodeAuthorError(
            "claude binary not found in PATH; install Claude Code in the worker image"
        )
    return found


def _compose_prompt(
    *,
    role: Role,
    task_title: str,
    task_description: str | None,
    plan_intent: str | None,
    prior_steps: list[PriorStep] | None = None,
) -> str:
    """Bundle the per-step inputs into a single prompt string.

    Skills (ordered) become a context block; hooks are not yet honored
    at v0 (no Claude Code hook injection wiring) — they ship via a future
    ADR. Format keeps the LLM's expectations clear: each section is
    headed by a Markdown ``##``.

    Multi-step workflows (per ADR-0015): when ``prior_steps`` is
    non-empty the most recent prior step's output is prepended as a
    "Prior step output" section. The analyzer→action contract lives in
    ``prior_steps[-1].output.payload.task_directive`` (a convention,
    not a typed field per ADR-0012 §"``payload``"); when that key is
    present we fold the structured directive in. When it's absent
    (e.g. the prior step is itself an action), we still surface
    ``summary`` + ``decision`` so the current role has *some* context
    rather than dropping it on the floor.
    """
    parts: list[str] = []
    prior_steps = prior_steps or []
    if prior_steps:
        parts.append(_render_prior_step_block(prior_steps[-1]))
    if plan_intent:
        parts.append(f"## Plan intent\n\n{plan_intent}\n")
    parts.append(f"## Task\n\n**Title:** {task_title}\n")
    if task_description:
        parts.append(f"\n**Description:**\n\n{task_description}\n")
    if role.skills:
        parts.append("\n## Skills available to you\n")
        for skill in role.skills:
            parts.append(f"\n### {skill.name}\n\n{skill.content}\n")
    parts.append(
        "\n## Instructions\n\n"
        "You are working inside a fresh git working tree at the current "
        "directory. Make the file changes required to satisfy the task. "
        "Use the tools available to you to read and edit files. When "
        "done, briefly summarize what you changed.\n"
    )
    return "".join(parts)


def _render_prior_step_block(prior: PriorStep) -> str:
    """Render the analyzer→action handoff block.

    The headline is the prior analyzer's ``summary`` + ``decision`` so
    the action role sees the human-readable framing. When the prior
    step's ``payload.task_directive`` is present (the analyzer
    convention per ADR-0015), the structured directive is folded in as
    JSON so the action role can read scope / files / intent literally.
    When it's absent (graceful fallback for action-step prior outputs
    or analyzer outputs whose ``decision`` is ``blocked`` /
    ``no-action-needed``), we still surface ``summary`` + ``decision``
    so the current role has context.
    """
    output = prior.output or {}
    summary = output.get("summary") or "(no summary)"
    decision = output.get("decision") or "(no decision)"
    payload = output.get("payload") or {}
    task_directive = payload.get("task_directive") if isinstance(payload, dict) else None

    lines = [
        "## Prior step output\n",
        f"\n**Prior step:** {prior.step_name} (role: {prior.role_id})\n",
        f"\n**Summary:** {summary}\n",
        f"\n**Decision:** {decision}\n",
    ]
    if task_directive is not None:
        # Serialize as JSON so the action role can parse if needed; the
        # surrounding ```json fence is a hint to the LLM that this is a
        # structured block, not free prose.
        lines.append(
            "\n**Task directive (from analyzer):**\n\n"
            "```json\n"
            f"{json.dumps(task_directive, indent=2, sort_keys=True)}\n"
            "```\n"
        )
    lines.append("\n")
    return "".join(lines)
