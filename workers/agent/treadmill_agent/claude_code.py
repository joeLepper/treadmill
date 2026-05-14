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
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

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


def run_claude(
    *,
    prompt: str,
    model: str,
    timeout_seconds: int = 30,
) -> str:
    """Invoke Claude with a prompt and model, returning raw output.

    Lightweight wrapper for simple Claude invocations that don't need
    the full role machinery. Used by validation_runtime for LLM-judge
    checks and other simple evaluation tasks.

    Args:
        prompt: The prompt to send to Claude
        model: Model ID (e.g., 'claude-haiku-4-5-20251001')
        timeout_seconds: Timeout for Claude execution

    Returns:
        Raw stdout output from Claude

    Raises:
        CodeAuthorError: If Claude exits non-zero or times out
    """
    binary = _find_binary()
    cmd = [
        binary, "--print",
        "--model", model,
        prompt,
    ]
    logger.info("running claude: model=%s", model)

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    assert proc.stdout is not None and proc.stderr is not None
    stdout_thread = threading.Thread(
        target=_pump_stream,
        args=(proc.stdout, stdout_lines, logging.INFO, "stdout", {}),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_pump_stream,
        args=(proc.stderr, stderr_lines, logging.WARNING, "stderr", {}),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
        returncode = proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        stdout_thread.join()
        stderr_thread.join()
        raise CodeAuthorError(f"Claude timed out after {timeout_seconds}s")
    stdout_thread.join()
    stderr_thread.join()

    stdout_text = "".join(stdout_lines)
    if returncode != 0:
        stderr_text = "".join(stderr_lines)
        raise CodeAuthorError(
            f"claude exited {returncode}\n"
            f"stdout:\n{stdout_text}\nstderr:\n{stderr_text}"
        )
    return stdout_text


def run_claude_code(
    *,
    repo_dir: Path,
    role: Role,
    task_title: str,
    task_description: str | None,
    plan_intent: str | None,
    prior_steps: list[PriorStep] | None = None,
    timeout_seconds: int = 1800,
    log_context: dict[str, Any] | None = None,
) -> CodeAuthorResult:
    """Drive Claude Code in ``repo_dir`` and return the captured summary.

    Timeout: 30 minutes (1800s) — sonnet 4.6 (operator-bumped 2026-05-14
    for role-code-author) thinks silently with ``--print`` mode and only
    flushes output at the end, so the worker sees no incremental
    progress. 600s was insufficient for substantive code-author tasks
    (e.g. authoring a disposition module + its tests); workers timed out
    mid-think and the SIGKILL killed real progress. 1800s buys headroom
    without unbounding runaway cost — combined with the per-task
    ``validation`` block (task #121) the operator still gets the
    failure surface, just on a longer wall-clock.

    The prompt bundles plan intent + task title + description + role's
    system_prompt + skill content + (for multi-step workflows) prior
    step outputs. Claude Code makes file edits directly in ``repo_dir``
    because the worker invokes it with ``cwd=repo_dir``.

    ``prior_steps`` is the ordered list of completed prior steps in the
    same run (per ADR-0015's ``prior_steps`` API extension). For
    two-step workflows the action role consumes the analyzer's
    ``task_directive`` from ``prior_steps[-1].output.payload``; the
    prompt-composer folds this in automatically.

    ``log_context`` is an optional dict of structured-logging fields the
    caller wants attached to every line streamed from the subprocess —
    typically ``task_id`` / ``step_id`` / ``role`` / ``model``. Per
    ADR-0020's "stream-and-tag, not capture-and-summarize" rule, the
    subprocess stdout/stderr is read line-by-line on background threads
    and each line is emitted via the package logger with these fields in
    ``extra`` (the bare ``message`` stays the raw line so ``docker logs
    -f`` is legible). The accumulated stdout is still joined and
    returned as ``CodeAuthorResult.summary`` so callers don't change.
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
    base_extra: dict[str, Any] = dict(log_context or {})
    logger.info(
        "running claude code: model=%s cwd=%s", role.model, repo_dir,
        extra=base_extra,
    )

    # ADR-0020 phase 2: stream stdout/stderr line-by-line instead of
    # ``capture_output=True``. The reader threads write into
    # ``stdout_lines`` / ``stderr_lines``; the main thread reads those
    # lists only after ``thread.join()`` so no lock is needed.
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    proc = subprocess.Popen(
        cmd, cwd=str(repo_dir),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    assert proc.stdout is not None and proc.stderr is not None
    stdout_thread = threading.Thread(
        target=_pump_stream,
        args=(proc.stdout, stdout_lines, logging.INFO, "stdout", base_extra),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_pump_stream,
        args=(proc.stderr, stderr_lines, logging.WARNING, "stderr", base_extra),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
        returncode = proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        # Kill the process so the reader threads see EOF and exit; then
        # join them so any buffered lines land in our accumulators before
        # the caller observes the failure. Re-raise so the runner can map
        # this to ``step.failed``.
        proc.kill()
        proc.wait()
        stdout_thread.join()
        stderr_thread.join()
        raise
    stdout_thread.join()
    stderr_thread.join()

    stdout_text = "".join(stdout_lines)
    stderr_text = "".join(stderr_lines)
    if returncode != 0:
        raise CodeAuthorError(
            f"claude exited {returncode}\n"
            f"stdout:\n{stdout_text}\nstderr:\n{stderr_text}"
        )
    return CodeAuthorResult(summary=stdout_text.strip() or "(no summary)")


def _pump_stream(
    stream: IO[str],
    accumulator: list[str],
    level: int,
    stream_name: str,
    base_extra: dict[str, Any],
) -> None:
    """Drain ``stream`` line-by-line, append each line to ``accumulator``,
    and emit each line via the package logger with ``stream=<name>`` and
    the caller's ``base_extra`` fields attached.

    ``bufsize=1`` (line-buffered) on the parent ``Popen`` means each line
    is delivered as soon as the child flushes. We accumulate the raw
    line *with* its trailing newline so the joined string round-trips
    byte-for-byte against the legacy ``result.stdout`` contract; the
    logged message is ``rstrip``'d so the visible log doesn't end with
    a redundant blank line. This function is the ``target=`` of a
    daemon thread — only this thread writes ``accumulator``; the main
    thread reads it only after ``thread.join()``.
    """
    try:
        for line in stream:
            accumulator.append(line)
            extra = dict(base_extra)
            extra["stream"] = stream_name
            logger.log(level, line.rstrip("\n"), extra=extra)
    finally:
        stream.close()


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
