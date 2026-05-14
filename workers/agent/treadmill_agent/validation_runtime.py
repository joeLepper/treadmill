"""Validation runtime — deterministic + LLM-judge check execution.

Per ADR-0029, the worker's wf-validate disposition runs two kinds of
validation checks:

  * **Deterministic checks:** shell scripts (exit 0 = pass, non-zero = fail).
  * **LLM-judge checks:** Claude evaluates prose criteria against the PR diff.

This module provides the low-level primitives to execute each kind and
collect the result into a typed ``CheckResult`` envelope.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger("treadmill.agent.validation_runtime")


@dataclass(frozen=True)
class CheckResult:
    """Structured envelope for a single check's execution result.

    Per ADR-0027 conventions, ``verdict`` is a closed ``Literal`` so
    callers cannot accidentally use an out-of-spec value. ``rationale``
    is required and capped at 4000 chars to match ReviewVerdict's
    constraint (cheap insurance against runaway model output).
    """

    check_id: str
    kind: str  # 'deterministic' | 'llm-judge'
    severity: str  # 'blocking' | 'warning' | 'advisory'
    verdict: str  # 'pass' | 'fail' | 'error'
    rationale: str  # human-readable why
    log_excerpt: str  # last ~2000 chars of subprocess stderr or LLM rationale


class ValidationVerdict(BaseModel):
    """Structured envelope for LLM-judge check output.

    Sibling to ``ReviewVerdict`` (ADR-0027). The LLM is instructed to
    emit a JSON block with ``verdict`` and ``rationale`` fields; this
    model validates the output shape.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["pass", "fail"]
    rationale: str = Field(..., max_length=4000)


def run_deterministic(
    check: Any,
    repo_dir: Path,
    timeout_seconds: int,
) -> CheckResult:
    """Execute a deterministic (shell script) validation check.

    Runs ``check.script`` via ``subprocess.run`` with shell=True,
    cwd=repo_dir. Captures stdout and stderr.

      * Exit 0 → verdict='pass'
      * Non-zero exit → verdict='fail'
      * TimeoutExpired → verdict='error' with rationale explaining timeout
      * Other exceptions → verdict='error'

    Args:
        check: object with .id, .kind, .severity, .script attributes
        repo_dir: working directory for the subprocess
        timeout_seconds: timeout for subprocess execution

    Returns:
        CheckResult with verdict, rationale, and log excerpt
    """
    try:
        result = subprocess.run(
            check.script,
            shell=True,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        stderr_excerpt = result.stderr[-2000:] if result.stderr else ""
        if result.returncode == 0:
            return CheckResult(
                check_id=check.id,
                kind=check.kind,
                severity=check.severity,
                verdict="pass",
                rationale=f"Script exited 0: {check.script}",
                log_excerpt=stderr_excerpt,
            )
        else:
            return CheckResult(
                check_id=check.id,
                kind=check.kind,
                severity=check.severity,
                verdict="fail",
                rationale=f"Script exited {result.returncode}: {check.script}",
                log_excerpt=stderr_excerpt,
            )
    except subprocess.TimeoutExpired:
        return CheckResult(
            check_id=check.id,
            kind=check.kind,
            severity=check.severity,
            verdict="error",
            rationale=f"Timeout after {timeout_seconds}s: {check.script}",
            log_excerpt="",
        )
    except Exception as e:
        return CheckResult(
            check_id=check.id,
            kind=check.kind,
            severity=check.severity,
            verdict="error",
            rationale=f"Exception: {type(e).__name__}: {str(e)}",
            log_excerpt="",
        )


def run_llm_judge(
    check: Any,
    repo_dir: Path,
    diff: str,
    task_spec: str,
    model: str,
    timeout_seconds: int,
) -> CheckResult:
    """Execute an LLM-judge validation check.

    Composes a prompt from check.prompt, diff, and task_spec, then
    spawns Claude Code to evaluate it. Parses the output as a JSON
    envelope (ValidationVerdict). Verdict 'pass' / 'fail' from the
    parsed model; parse failure or unknown verdict → error.

    Args:
        check: object with .id, .kind, .severity, .prompt attributes
        repo_dir: working directory (for context; not used by Claude)
        diff: PR diff text to evaluate
        task_spec: task specification text
        model: Claude model to use (e.g., 'claude-haiku-4-5-20251001')
        timeout_seconds: timeout for Claude execution

    Returns:
        CheckResult with verdict, rationale, and log excerpt
    """
    from treadmill_agent import claude_code

    prompt = (
        f"{check.prompt}\n\n"
        f"## PR diff\n{diff}\n\n"
        f"## Task spec\n{task_spec}\n\n"
        f"Respond with a JSON block containing 'verdict' ('pass' or 'fail') "
        f"and 'rationale' (human-readable explanation)."
    )

    try:
        result = claude_code.run_claude(
            prompt=prompt,
            model=model,
            timeout_seconds=timeout_seconds,
        )
        # Parse the JSON block from the result
        verdict, rationale = _parse_validation_envelope(result)
        return CheckResult(
            check_id=check.id,
            kind=check.kind,
            severity=check.severity,
            verdict=verdict,
            rationale=rationale,
            log_excerpt=rationale[-2000:] if rationale else "",
        )
    except Exception as e:
        return CheckResult(
            check_id=check.id,
            kind=check.kind,
            severity=check.severity,
            verdict="error",
            rationale=f"Exception: {type(e).__name__}: {str(e)}",
            log_excerpt="",
        )


def _parse_validation_envelope(
    output: str,
) -> tuple[str, str]:
    """Parse LLM output into (verdict, rationale).

    Extracts the last JSON fence block from the output, parses it as
    ValidationVerdict, and returns the typed values. On parse failure,
    returns ('error', error_message).

    Args:
        output: raw Claude output

    Returns:
        (verdict, rationale) tuple; verdict is 'pass', 'fail', or 'error'
    """
    import re

    _JSON_FENCE_RE = re.compile(
        r"```json5?\s*\n(.*?)\n```",
        flags=re.DOTALL | re.IGNORECASE,
    )

    matches = _JSON_FENCE_RE.findall(output or "")
    if not matches:
        return ("error", "No JSON fence found in LLM output")

    block = matches[-1]
    try:
        data = json.loads(block)
        parsed = ValidationVerdict.model_validate(data)
        return (parsed.verdict, parsed.rationale)
    except (json.JSONDecodeError, ValidationError) as exc:
        return ("error", f"JSON parse failed: {str(exc)[:200]}")
