"""Plan-doc validator: mechanically check sequence_of_work tasks against
the ``.claude/skills/plan/SKILL.md`` authoring rules.

Background — three plan-authoring rule violations shipped in a single
session on 2026-05-28 (ADR-0059 Step 1 + Step 4), each requiring a
cancel-and-retry cycle: ``alembic upgrade head`` (sandbox violation),
``test -f .../20260528_1600_<name>.py`` (exact-filename pin), and
``cd /home/joe/treadmill/workers/agent`` (absolute path that doesn't
exist in the worker sandbox). Manual self-discipline against SKILL.md
failed reliably under session length; the structural fix is to
automate the rule check at authoring time.

This module is pure: ``validate_plan_doc(markdown_text)`` returns a
list of :class:`Violation` records. The Typer wrapper lives in
:mod:`treadmill_cli.cli` and presents them to the operator.

Rules encoded (SKILL.md ~line 132):

- **Sandbox-safety:** every tool a ``deterministic`` gate invokes must
  exist in the worker sandbox — no live AWS / Docker / Postgres /
  package-registry egress, no absolute paths to dev-machine
  directories.
- **Format-robustness:** ``deterministic`` gates that exact-string-grep
  multi-arg call signatures or exact filenames with author-chosen
  timestamps fail on valid-but-differently-formatted code; flag them.

The validator runs on the parsed AST (``TaskSpec`` list) from
:func:`treadmill_api.parsers.plan_doc.parse_plan_doc` so the surface
matches what the API enforces at submission time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from treadmill_api.parsers.plan_doc import TaskSpec


# ── Public types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Violation:
    """A single rule violation found in a plan doc.

    ``validation_index`` is the 0-based index into the task's
    ``validation`` list when the offending site is a validation check;
    ``None`` when the site is task-level (e.g. ``scope.files``).

    ``rule`` is a short code (``sandbox-unsafe-tool``,
    ``format-brittle-filename``, ``absolute-path``, ...) suitable for
    grep + tooling. ``detail`` is the human-readable message.
    ``citation`` points back to the SKILL.md rule the violation breaks.
    """

    task_id: str
    validation_index: int | None
    rule: str
    detail: str
    citation: str


# ── Rule patterns ────────────────────────────────────────────────────────────


_SANDBOX_UNSAFE_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    # CDK against live AWS.
    (
        re.compile(r"\bcdk\s+(synth|deploy|diff|bootstrap)\b"),
        "sandbox-unsafe-tool",
        "`cdk` subcommand requires live AWS / synth context unavailable in worker sandbox",
    ),
    # AWS CLI hitting a live endpoint.
    (
        re.compile(r"\baws\s+\w+\s+\w+"),
        "sandbox-unsafe-tool",
        "`aws ...` calls require live AWS credentials + network egress, both blocked in worker sandbox",
    ),
    # Docker daemon access.
    (
        re.compile(r"\bdocker\s+(run|exec|compose|build|pull|push)\b"),
        "sandbox-unsafe-tool",
        "`docker` subcommands require a daemon socket the worker sandbox doesn't expose",
    ),
    # Live Postgres.
    (
        re.compile(r"\bpsql\b"),
        "sandbox-unsafe-tool",
        "`psql` requires a live DB the worker sandbox doesn't provide",
    ),
    # Live DB-bound alembic.
    (
        re.compile(r"\balembic\s+(upgrade|downgrade|stamp|history|current)\b"),
        "sandbox-unsafe-tool",
        "`alembic upgrade/downgrade/stamp/history/current` needs DATABASE_URL not set in worker sandbox; gate the migration via pytest fixtures instead",
    ),
    # Package-registry installs (egress).
    (
        re.compile(r"\bpip\s+install\b(?!\s+-e\s+\.|\s+\.)"),
        "sandbox-unsafe-tool",
        "`pip install <pkg>` needs network egress to PyPI; the worker has no registry access in v1",
    ),
    (
        re.compile(r"\bnpm\s+install\b(?!\s*$|\s*&&)"),
        "sandbox-unsafe-tool",
        "`npm install <pkg>` needs network egress to npm registry; the worker has no registry access in v1",
    ),
    # curl/wget against an external URL.
    (
        re.compile(r"\b(curl|wget)\b[^\n]*\bhttps?://(?!(localhost|127\.0\.0\.1|0\.0\.0\.0))"),
        "sandbox-unsafe-tool",
        "`curl`/`wget` against an external URL needs network egress blocked in worker sandbox",
    ),
)

# Absolute paths that point at dev-machine layout the worker sandbox
# does not have. The worker's repo lives under
# ``/var/treadmill/workspaces/<uuid>/repo`` — everything else is
# unavailable.
_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w/])(/home/|/Users/|/root/|/opt/treadmill/)")

# Exact-filename pin that the task author may freely re-time. Matches
# ``test -f`` (or any file-test) followed by a path containing a
# ``YYYYMMDD_HHMM`` segment — Alembic migration filenames and similar
# timestamped artifacts are author-chosen and will not match an exact
# string. Pattern matches the timestamp anywhere on the same logical
# line as the file test so reasonable variations still flag.
_FORMAT_BRITTLE_FILENAME_RE = re.compile(
    r"\btest\s+-[a-z]+\s+[^\n]*?\d{8}_\d{4}[^\n]*?\.py\b"
)

# ``grep -q`` (or similar) for a multi-arg call signature with named
# args — e.g. ``grep -q "func(a=1, b=2)"``. Heuristic: a grep pattern
# containing both ``(`` and ``=`` and ``,`` inside the matched literal.
# This is intentionally narrow — we'd rather under-flag than chase the
# author about a benign exact-match.
_FORMAT_BRITTLE_GREP_RE = re.compile(
    r"\bgrep\s+[^\n]*?-[a-zA-Z]*[qE][a-zA-Z]*[^\n]*?['\"][^'\"]*\([^'\")]*=[^'\")]*,[^'\")]*\)[^'\"]*['\"]"
)


# ── Public API ───────────────────────────────────────────────────────────────


def validate_plan_doc(markdown_text: str) -> list[Violation]:
    """Parse ``markdown_text`` as a plan doc and return rule violations.

    Returns an empty list when the plan is clean. Raises
    :class:`treadmill_api.parsers.plan_doc.PlanDocFormatError` (or
    :class:`pydantic.ValidationError`) when the doc itself does not
    parse — the validator does not invent task specs.
    """
    from treadmill_api.parsers.plan_doc import parse_plan_doc

    specs = parse_plan_doc(markdown_text)
    violations: list[Violation] = []
    for spec in specs:
        violations.extend(_validate_task(spec))
    return violations


def _validate_task(spec: "TaskSpec") -> list[Violation]:
    """Run all rules against a single task spec; aggregate violations."""
    out: list[Violation] = []
    for absolute_path_offender in _find_absolute_paths(spec.scope.files):
        out.append(
            Violation(
                task_id=spec.id,
                validation_index=None,
                rule="absolute-path",
                detail=(
                    f"scope.files contains absolute path {absolute_path_offender!r}; "
                    f"the worker checks out the repo at "
                    f"/var/treadmill/workspaces/<uuid>/repo and scope.files paths "
                    f"are relative to that root"
                ),
                citation="SKILL.md — scope discipline (paths are repo-relative)",
            )
        )
    for idx, check in enumerate(spec.validation):
        if check.kind != "deterministic":
            continue
        script = check.script or ""
        out.extend(_validate_deterministic_script(spec.id, idx, script))
    return out


def _validate_deterministic_script(
    task_id: str, validation_index: int, script: str,
) -> list[Violation]:
    """Run sandbox-safety + format-robustness rules against one script."""
    out: list[Violation] = []
    # Sandbox + absolute-path checks treat quoted-string contents as
    # *search input*, not as commands: ``grep "pip install" Dockerfile``
    # is a meta-check for the *string*, not an invocation of ``pip
    # install``. Strip quoted bodies before matching to avoid that
    # false positive. The format-brittleness rules below run on the
    # *original* script — they're specifically about content inside
    # grep patterns and test arguments.
    script_unquoted = _strip_quoted_strings(script)
    for pattern, rule, detail in _SANDBOX_UNSAFE_PATTERNS:
        if pattern.search(script_unquoted):
            out.append(
                Violation(
                    task_id=task_id,
                    validation_index=validation_index,
                    rule=rule,
                    detail=detail,
                    citation="SKILL.md — Every tool the gate invokes must exist in the worker sandbox",
                )
            )
    for offender in _ABSOLUTE_PATH_RE.findall(script_unquoted):
        out.append(
            Violation(
                task_id=task_id,
                validation_index=validation_index,
                rule="absolute-path",
                detail=(
                    f"script references absolute path prefix {offender!r}; "
                    f"the worker sandbox does not have this layout"
                ),
                citation="SKILL.md — Every tool the gate invokes must exist in the worker sandbox",
            )
        )
    if _FORMAT_BRITTLE_FILENAME_RE.search(script):
        out.append(
            Violation(
                task_id=task_id,
                validation_index=validation_index,
                rule="format-brittle-filename",
                detail=(
                    "script pins an exact filename containing a YYYYMMDD_HHMM "
                    "timestamp; the author may legitimately choose a different "
                    "timestamp. Replace with a content-grep that targets the "
                    "invariant rather than the filename"
                ),
                citation="SKILL.md — Make `deterministic` validation robust, not formatting-brittle",
            )
        )
    if _FORMAT_BRITTLE_GREP_RE.search(script):
        out.append(
            Violation(
                task_id=task_id,
                validation_index=validation_index,
                rule="format-brittle-grep",
                detail=(
                    "script greps for a multi-arg call signature; valid "
                    "reformats (line wrap, arg reorder, trailing comma) "
                    "would fail the match. Grep for the function name only, "
                    "or assert behavior via pytest"
                ),
                citation="SKILL.md — Make `deterministic` validation robust, not formatting-brittle",
            )
        )
    return out


def _strip_quoted_strings(text: str) -> str:
    """Replace ``"..."`` and ``'...'`` bodies with empty quote pairs.

    Used by sandbox-safety + absolute-path checks so that strings
    appearing as *search input* (``grep "pip install" Dockerfile``)
    don't fire rules meant for *invocations* (``pip install foo``).

    Trade-off: a ``cd "/absolute/path"`` is no longer caught (the
    path is inside quotes). In practice ``cd`` is almost never
    quoted; the trade is worth it to keep grep meta-checks clean.
    """
    text = re.sub(r'"[^"\n]*"', '""', text)
    text = re.sub(r"'[^'\n]*'", "''", text)
    return text


def _find_absolute_paths(file_paths: list[str]) -> list[str]:
    """Return entries of ``file_paths`` that begin with ``/``.

    The worker resolves ``scope.files`` against the workspace repo
    root; an absolute path either escapes the workspace or fails the
    scope check at gate time.
    """
    return [p for p in file_paths if p.startswith("/")]
