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

import contextvars
import json
import logging
import os
import shutil
import subprocess
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, TYPE_CHECKING

from treadmill_agent.api_client import PriorStep, Role
from treadmill_agent import observability

if TYPE_CHECKING:
    from treadmill_agent.startup_auth import ClaudeCreds

logger = logging.getLogger("treadmill.agent.claude_code")


# ADR-0055 — per-step Claude credential routing. The runner sets this around
# ``_execute`` (via :func:`set_claude_creds`) so every ``Popen`` site that
# launches Claude — ``run_claude``, ``run_claude_code``, plus the calls from
# ``validation_runtime`` / ``judge_eval`` / dispositions — picks up the right
# account's token without each call site growing a kwarg. ``None`` (the
# default) preserves the legacy ``CLAUDE_CREDENTIALS_PATH`` bind-mount.
_CURRENT_CREDS: contextvars.ContextVar["ClaudeCreds | None"] = (
    contextvars.ContextVar("claude_creds", default=None)
)


def set_claude_creds(creds: "ClaudeCreds | None"):
    """Bind ``creds`` for the calling context; returns a token for ``reset``."""
    return _CURRENT_CREDS.set(creds)


def reset_claude_creds(token) -> None:
    """Undo a prior :func:`set_claude_creds`; pair them in ``try/finally``."""
    _CURRENT_CREDS.reset(token)


@dataclass(frozen=True)
class CodeAuthorResult:
    summary: str
    """A short text summary produced by Claude Code (its ``--print`` output).
    Stored on ``step.output.summary`` so the user can see what changed
    without diffing."""

    token_usage: dict[str, int] | None = None
    """Per-step token counters parsed from Claude Code's JSON envelope:
    ``input_tokens``, ``output_tokens``, ``cache_creation_tokens``,
    ``cache_read_tokens``. ``None`` when the step made no LLM call
    (dry-run, wf-validate) or the stub binary didn't emit a JSON
    ``usage`` block. Threaded up to the runner so ``step.completed``
    can persist it (ADR-0020 §"Token tracking"); the OTel emission via
    :func:`observability.record_token_usage` stays unchanged."""

    model: str | None = None
    """The role's model id (e.g. ``claude-opus-4-7``) the run executed
    against. Paired with ``token_usage`` so the API can attribute usage
    rows to a specific model; ``None`` when ``token_usage`` is ``None``."""


class CodeAuthorError(RuntimeError):
    """Surface non-zero exit codes from the Claude Code CLI."""


def build_claude_env(
    parent_env: Mapping[str, str], creds: "ClaudeCreds | None",
) -> dict[str, str]:
    """Build the child env for a Claude Code ``Popen`` (ADR-0055).

    ``creds=None`` (no per-account routing) returns the parent env unchanged
    — the existing ``CLAUDE_CREDENTIALS_PATH`` bind-mount stays in effect.

    With creds, we clear any inherited auth-bearing env vars *and* the
    file-mount path so the child sees exactly one credential of the chosen
    type:

      * ``oauth``   → ``CLAUDE_CODE_OAUTH_TOKEN``
      * ``api_key`` → ``ANTHROPIC_API_KEY``

    This is what makes "no silent cross-account fallback" true at the
    process boundary, not just the API one.
    """
    env = dict(parent_env)
    if creds is None:
        return env
    # Clear every auth env Claude Code recognises, then set exactly one.
    for key in (
        "CLAUDE_CREDENTIALS_PATH",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
    ):
        env.pop(key, None)
    if creds.type == "oauth":
        env["CLAUDE_CODE_OAUTH_TOKEN"] = creds.token
    elif creds.type == "api_key":
        env["ANTHROPIC_API_KEY"] = creds.token
    else:  # pragma: no cover — Pydantic on the API side rejects this
        raise ValueError(f"unknown claude_account type: {creds.type!r}")
    return env


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

    Credential routing per ADR-0055: the subprocess env is built from
    :data:`_CURRENT_CREDS` (set by the runner around ``_execute``); when
    unset, the legacy ``CLAUDE_CREDENTIALS_PATH`` mount path stays in scope.

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
        env=build_claude_env(os.environ, _CURRENT_CREDS.get()),
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
        # JSON output mode: claude emits a single JSON object on stdout
        # once the run completes. We parse it for token usage metrics
        # (ADR-0020 §"Token tracking"). Trade-off: the real-time
        # line-by-line text streaming visible in Grafana during the run
        # is replaced by one JSON blob at the end — the Popen+threads
        # code structure from ADR-0020 phase 2 is preserved, but the
        # incremental output doesn't arrive until the run finishes.
        "--output-format", "json",
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
        env=build_claude_env(os.environ, _CURRENT_CREDS.get()),
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

    summary, usage = _try_parse_json_output(stdout_text)
    if usage is not None:
        ctx = log_context or {}
        try:
            observability.record_token_usage(
                model=role.model,
                role=role.id,
                task_id=str(ctx.get("task_id", "")),
                step_id=str(ctx.get("step_id", "")),
                **usage,
            )
        except Exception:
            logger.debug(
                "token usage metric emission failed (non-fatal)",
                extra=base_extra,
            )
    else:
        logger.debug(
            "claude stdout is not JSON; token metrics not emitted",
            extra=base_extra,
        )

    return CodeAuthorResult(
        summary=summary.strip() or "(no summary)",
        token_usage=usage,
        # Pair the model with token_usage so a downstream consumer
        # never has to guess which model the counters belong to. When
        # ``usage`` is None (no LLM call observed) the model is also
        # None — the API persists NULLs for both.
        model=role.model if usage is not None else None,
    )


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


def _try_parse_json_output(
    text: str,
) -> tuple[str, dict[str, int] | None]:
    """Parse claude's JSON output (``--output-format json``) and extract
    the result text plus token usage counters.

    Returns ``(summary, usage)`` where ``usage`` is a dict with keys
    ``input_tokens``, ``output_tokens``, ``cache_creation_tokens``,
    ``cache_read_tokens`` (all ``int``), or ``None`` when the stdout is
    not JSON (e.g. stub binaries in unit tests) or lacks a ``usage``
    block.

    The expected JSON shape from Claude Code 2.x is::

        {
          "type": "result",
          "subtype": "success",
          "result": "<text the role produced>",
          "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0
          },
          ...
        }
    """
    stripped = text.strip()
    if not stripped:
        return text, None
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return text, None

    if not isinstance(data, dict):
        return text, None

    result_text = data.get("result", "")
    if not isinstance(result_text, str):
        result_text = str(result_text)

    usage_raw = data.get("usage")
    if not isinstance(usage_raw, dict):
        return result_text or text, None

    usage = {
        "input_tokens": int(usage_raw.get("input_tokens", 0)),
        "output_tokens": int(usage_raw.get("output_tokens", 0)),
        # JSON key is cache_creation_input_tokens; OTel attr is cache_creation_tokens
        "cache_creation_tokens": int(usage_raw.get("cache_creation_input_tokens", 0)),
        "cache_read_tokens": int(usage_raw.get("cache_read_input_tokens", 0)),
    }
    return result_text or text, usage


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
