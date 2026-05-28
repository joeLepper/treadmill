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
    pr_number: int | None = None,
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
        pr_number: when set, exported as ``PR_NUMBER`` in the subprocess
            env so rule scripts can resolve the active PR without having
            to auto-detect from the working tree (which is unreliable in
            worker containers).

    Returns:
        CheckResult with verdict, rationale, and log excerpt
    """
    try:
        import os

        from treadmill_agent.repo_deps import current_overlay

        env: dict[str, str] = dict(os.environ)
        overlay = current_overlay()
        if overlay is not None:
            for k, v in overlay.env_overrides().items():
                env[k] = v
        if pr_number is not None:
            env["PR_NUMBER"] = str(pr_number)
        result = subprocess.run(
            check.script,
            shell=True,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
        )
        combined = ""
        if result.stdout:
            combined += "--- stdout ---\n" + result.stdout
        if result.stderr:
            if combined:
                combined += "\n"
            combined += "--- stderr ---\n" + result.stderr
        excerpt = combined[-4000:] if combined else ""
        if result.returncode == 0:
            return CheckResult(
                check_id=check.id,
                kind=check.kind,
                severity=check.severity,
                verdict="pass",
                rationale=f"Script exited 0: {check.script}",
                log_excerpt=excerpt,
            )
        # pytest exit 5 = "no tests collected" — pytest 7+ unifies the
        # missing-file and empty-suite cases here. Observed 2026-05-16
        # on Plan A/B stuck tasks: the role-code-author often writes
        # tests at a path the plan-spec's validation script doesn't
        # match (or skips authoring tests entirely while shipping the
        # implementation). Treating that as a blocking fail dead-ends
        # the task while the actual implementation is fine. Demote to
        # pass with rationale noting the gap so an operator at PR
        # review can correct the test placement.
        #
        # Detection is signal-driven (stdout contains "no tests ran")
        # rather than exit-code-based (pytest emits 5 in some configs,
        # 4 in others; we've seen both). Exit code is preserved in
        # rationale + log_excerpt for transparency.
        stdout_lower = (result.stdout or "").lower()
        if "no tests ran" in stdout_lower or "no tests collected" in stdout_lower:
            return CheckResult(
                check_id=check.id,
                kind=check.kind,
                severity=check.severity,
                verdict="pass",
                rationale=(
                    f"Script exited {result.returncode} but stdout reports "
                    "'no tests ran' (pytest exit 5 / 4 semantics — missing "
                    "test file or empty suite). Demoting to pass: the "
                    "implementation is unaffected; operator review on the "
                    "PR can flag if a test file was meant to ship here. "
                    f"Script: {check.script}"
                ),
                log_excerpt=excerpt,
            )
        return CheckResult(
            check_id=check.id,
            kind=check.kind,
            severity=check.severity,
            verdict="fail",
            rationale=f"Script exited {result.returncode}: {check.script}",
            log_excerpt=excerpt,
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


def gather_agent_md_context(repo_dir: Path, diff: str) -> str:
    """Return the content of every AGENT.md that governs a file touched
    by ``diff`` — the nearest AGENT.md walking up from each touched
    path to ``repo_dir``. Empty string if none. Repo-agnostic (no
    dependency on a rule file) so it works for any onboarded repo."""
    if not diff:
        return ""

    repo_dir = Path(repo_dir).resolve()

    touched: list[Path] = []
    for line in diff.splitlines():
        if not line.startswith("+++ b/"):
            continue
        rel = line[len("+++ b/") :].strip()
        if not rel or rel == "/dev/null":
            continue
        touched.append(repo_dir / rel)

    agent_md_paths: list[Path] = []
    seen: set[Path] = set()
    for path in touched:
        cur = path.parent.resolve()
        while True:
            if cur != repo_dir and repo_dir not in cur.parents:
                break
            candidate = cur / "AGENT.md"
            if candidate.is_file():
                if candidate not in seen:
                    seen.add(candidate)
                    agent_md_paths.append(candidate)
                break
            if cur == repo_dir:
                break
            cur = cur.parent

    blocks: list[str] = []
    for agent_md in agent_md_paths:
        try:
            content = agent_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            relpath = agent_md.relative_to(repo_dir)
        except ValueError:
            continue
        blocks.append(f"### {relpath}\n{content}")

    return "\n\n".join(blocks)


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

    The composed prompt also includes the content of every AGENT.md
    that governs a file touched by ``diff`` (nearest ancestor walk),
    so judges like ADR-0030's docs-current-with-pr can see the
    component-level documentation they're asked to evaluate.

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

    agent_md = gather_agent_md_context(repo_dir, diff or "")
    agent_md_section = f"## AGENT_MD\n{agent_md}\n\n" if agent_md else ""

    prompt = (
        f"{check.prompt}\n\n"
        f"{agent_md_section}"
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
