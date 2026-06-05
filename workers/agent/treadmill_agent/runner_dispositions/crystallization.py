"""``crystallization`` disposition — handles wf-crystallize-learning workflow.

Two steps, one handler (dispatches on ``ctx.ctx.role.id``):

Step 1 — ``role-crystallization-judge`` parses a ``CrystallizationVerdict``
envelope (ADR-0027 pattern) from Claude's summary:

  * ``ready``     — emit dispatch payload routing to step 2 (architect).
  * ``not-ready`` — update the learning's YAML frontmatter with
                    ``last_crystallization_check`` / ``next_crystallization_check``
                    (exponential backoff: 1d → 3d → 7d → 14d → 30d) and
                    append the reasoning to its Notes section.  Commit + push
                    + open/update PR so the update is durable.
  * ``defer``     — no-op; the learning is re-evaluated on the next
                    crystallize run.

Step 2 — ``role-architect`` called from ``wf-crystallize-learning`` extracts
the rule YAML and check.sh from the architect's summary, writes them to:

  * ``docs/knowledge-base/rules/<slug>.yaml``
  * ``tools/rule-checks/<slug>/check.sh``

Then updates the source learning's ``status:`` to
``crystallized-into-rule-<slug>``, commits, pushes, opens PR.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from treadmill_api.events.crystallization_verdict import CrystallizationVerdict

from treadmill_agent.events import Artifact, Metadata, StepOutput
from treadmill_agent.runner_dispositions._context import DispositionContext

logger = logging.getLogger("treadmill.agent.crystallization")

_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_YAML_BLOCK_RE = re.compile(r"```yaml\s*(.*?)\s*```", re.DOTALL)
_BASH_BLOCK_RE = re.compile(r"```(?:bash|sh)\s*(.*?)\s*```", re.DOTALL)

_VALID_VERDICTS = frozenset({"ready", "not-ready", "defer"})

# Exponential backoff schedule for not-ready verdicts (in days).
# Index = crystallization_check_count before this check.
_BACKOFF_DAYS = [1, 3, 7, 14, 30]

# Prose-fallback verdict cues. Ordered by precedence — the disposition
# scans the model's summary for these phrases (lowercased) and assigns
# the matching verdict. Listed most-specific first to avoid false matches
# (e.g. "defer" appears inside "not-ready; defer until…").
#
# Observed: sonnet on the role-crystallization-judge prompt sometimes
# produces a thorough prose analysis but omits the JSON envelope at the
# close — even after the prompt's closing imperative. The strict parser
# raised CrystallizationVerdictParseError and burned the step attempt.
# This fallback extracts the model's intended verdict from prose so the
# system can act on it; the strict JSON path remains primary.
_PROSE_VERDICT_CUES: list[tuple[str, tuple[str, ...]]] = [
    ("ready", (
        "verdict: ready",
        "learning is ready",
        "ready to crystallize",
        "ready for crystallization",
        "promote to a rule",
        "promote this to a rule",
        "should be crystallized",
        "meets the bar for a rule",
        "qualifies as a rule",
    )),
    ("not-ready", (
        "verdict: not-ready",
        "not-ready",
        "not yet ready",
        "needs more evidence",
        "more observations needed",
        "too early to crystallize",
        "not enough data",
        "insufficient evidence to crystallize",
    )),
    ("defer", (
        "verdict: defer",
        "should be deferred",
        "re-evaluate later",
        "check again later",
        "skip this run",
        "come back to this",
    )),
]

# Regex to pull a learning slug from prose — matches patterns like
# ``learning_slug: my-slug`` or ``"learning_slug": "my-slug"``.
_SLUG_RE = re.compile(
    r'"?learning[_\s-]slug"?\s*[:=]\s*"?([a-z0-9][a-z0-9-]*)',
    re.IGNORECASE,
)


def _parse_verdict_from_prose(summary: str) -> dict[str, Any] | None:
    """Fallback: scan prose for verdict cues and synthesize an envelope dict.

    Returns the synthesized dict on success, or ``None`` if the summary is
    empty / blank (callers let the strict-parse error fire in that case).

    Unlike the architect's fallback, crystallization has no ``uncertain``
    catch-all — if no cue matches, ``None`` is returned so the caller can
    raise ``CrystallizationVerdictParseError`` rather than silently deferring
    an unrecognized summary.
    """
    if not summary.strip():
        return None
    lower = summary.lower()
    slug_match = _SLUG_RE.search(summary)
    learning_slug = slug_match.group(1) if slug_match else ""
    for verdict, cues in _PROSE_VERDICT_CUES:
        for cue in cues:
            if cue in lower:
                return {
                    "verdict": verdict,
                    "reasoning": (
                        "Extracted from judge prose (no JSON envelope emitted). "
                        f"Matched cue: {cue!r}."
                    ),
                    "learning_slug": learning_slug,
                    "proposed_rule_slug": None,
                }
    return None


class CrystallizationVerdictParseError(RuntimeError):
    """Raised when Claude's summary lacks a parsable CrystallizationVerdict.

    The runner's exception layer turns this into ``step.failed``; wf-feedback
    can re-run the judge with an explicit envelope reminder.
    """


# ── verdict envelope parsing ──────────────────────────────────────────────────


def _extract_verdict_envelope(summary: str) -> CrystallizationVerdict:
    """Return the last JSON block whose object has a valid ``verdict`` field,
    validated against the ``CrystallizationVerdict`` Pydantic model.

    Follows the ADR-0027 last-block-wins convention so a judge that explores
    alternatives in earlier blocks can converge to a final verdict at the end.

    Falls back to prose-cue parsing when no JSON envelope is present —
    mirrors the architect's fallback (architecture.py ``_parse_verdict_from_prose``).
    The strict JSON path remains primary; the prose path keeps the loop moving.
    """
    envelope: dict[str, Any] | None = None
    for m in _JSON_BLOCK_RE.finditer(summary):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("verdict") in _VALID_VERDICTS:
            envelope = data
    if envelope is None:
        fallback = _parse_verdict_from_prose(summary)
        if fallback is not None:
            logger.warning(
                "crystallization-judge: no JSON block found; extracted "
                "verdict %r from prose (cue matched). Prompt or model may "
                "need tightening.",
                fallback.get("verdict"),
            )
            envelope = fallback
        else:
            raise CrystallizationVerdictParseError(
                "crystallization-judge summary contained no JSON block with a valid "
                "``verdict`` field; expected one of: "
                + ", ".join(sorted(_VALID_VERDICTS))
            )
    try:
        return CrystallizationVerdict.model_validate(envelope)
    except ValidationError as exc:
        raise CrystallizationVerdictParseError(
            f"CrystallizationVerdict validation failed: {exc}"
        ) from exc


# ── learning file helpers ─────────────────────────────────────────────────────


def _find_learning_file(repo_dir: Path, slug: str) -> Path | None:
    """Locate a learning markdown file by slug.

    Searches ``docs/knowledge-base/learnings/`` then ``docs/learnings/``.
    Returns the first file whose name contains ``slug``, or None.
    """
    for learning_dir in (
        repo_dir / "docs" / "knowledge-base" / "learnings",
        repo_dir / "docs" / "learnings",
    ):
        if not learning_dir.exists():
            continue
        for path in learning_dir.glob(f"*{slug}*.md"):
            return path
    return None


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split YAML frontmatter from markdown body.

    Returns ``(frontmatter_dict, body)``.  Frontmatter values are plain
    strings (no nested YAML); sufficient for the scalar fields we read/write.
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm_raw = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")
    fm: dict[str, str] = {}
    for line in fm_raw.splitlines():
        if ": " in line:
            key, val = line.split(": ", 1)
            fm[key.strip()] = val.strip()
        elif line.endswith(":"):
            fm[line[:-1].strip()] = ""
    return fm, body


def _render_frontmatter(fm: dict[str, str], body: str) -> str:
    """Serialize ``fm`` + ``body`` back to a markdown string."""
    lines = ["---"]
    for key, val in fm.items():
        lines.append(f"{key}: {val}" if val else f"{key}:")
    lines.append("---")
    return "\n".join(lines) + "\n" + body


def _next_backoff_days(check_count: int) -> int:
    """Return the delay (in days) before the next crystallization attempt."""
    idx = min(check_count, len(_BACKOFF_DAYS) - 1)
    return _BACKOFF_DAYS[idx]


def _update_learning_not_ready(
    learning_path: Path,
    *,
    reasoning: str,
    check_count: int,
) -> None:
    """Update frontmatter for a not-ready verdict and append reasoning to Notes."""
    text = learning_path.read_text()
    fm, body = _parse_frontmatter(text)

    today = date.today()
    next_check = today + timedelta(days=_next_backoff_days(check_count))
    fm["last_crystallization_check"] = today.isoformat()
    fm["next_crystallization_check"] = next_check.isoformat()
    fm["crystallization_check_count"] = str(check_count + 1)

    note = f"- {today.isoformat()} not-ready: {reasoning.strip()}"
    if "## Notes" in body:
        body = body.rstrip("\n") + f"\n{note}\n"
    else:
        body = body.rstrip("\n") + f"\n\n## Notes\n\n{note}\n"

    learning_path.write_text(_render_frontmatter(fm, body))


def _update_learning_crystallized(learning_path: Path, *, rule_slug: str) -> None:
    """Set ``status: crystallized-into-rule-<slug>`` in the learning's frontmatter."""
    text = learning_path.read_text()
    fm, body = _parse_frontmatter(text)
    fm["status"] = f"crystallized-into-rule-{rule_slug}"
    learning_path.write_text(_render_frontmatter(fm, body))


# ── architect output extraction ───────────────────────────────────────────────


def _extract_rule_yaml(summary: str) -> str | None:
    """Return the last ```yaml block from the architect's summary, or None."""
    matches = _YAML_BLOCK_RE.findall(summary)
    return matches[-1].strip() if matches else None


def _extract_check_sh(summary: str) -> str | None:
    """Return the last ```bash / ```sh block from the architect's summary."""
    matches = _BASH_BLOCK_RE.findall(summary)
    return matches[-1].strip() if matches else None


def _slug_from_yaml(yaml_text: str) -> str:
    """Extract the ``name:`` field from a rule YAML string."""
    for line in yaml_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("name:"):
            return stripped.split(":", 1)[1].strip()
    return ""


# ── step handlers ─────────────────────────────────────────────────────────────


def _handle_judge(ctx: DispositionContext) -> StepOutput:
    """Step 1: parse CrystallizationVerdict and route on verdict."""
    from treadmill_agent import git
    from treadmill_agent.runner import _commit_message  # local import avoids cycle

    summary = ctx.claude_result.summary or ""
    verdict_obj = _extract_verdict_envelope(summary)

    verdict = verdict_obj.verdict
    reasoning = verdict_obj.reasoning
    learning_slug = verdict_obj.learning_slug
    proposed_rule_slug = verdict_obj.proposed_rule_slug

    logger.info(
        "crystallization-judge verdict=%s learning=%s rule=%s",
        verdict, learning_slug, proposed_rule_slug,
    )

    if verdict == "ready":
        payload: dict[str, Any] = {
            "verdict": verdict,
            "reasoning": reasoning,
            "learning_slug": learning_slug,
            "proposed_rule_slug": proposed_rule_slug,
            "dispatch": {
                "workflow_id": "wf-crystallize-learning",
                "step": "crystallize",
                "learning_slug": learning_slug,
                "proposed_rule_slug": proposed_rule_slug,
                "task_id": ctx.ctx.task_id,
            },
        }
        return StepOutput(
            summary=summary,
            decision="ready",
            commit_sha=None,
            artifacts=[Artifact(kind="analysis", value=summary)],
            payload=payload,
            metadata=Metadata(),
        )

    if verdict == "not-ready":
        learning_path = _find_learning_file(ctx.repo_dir, learning_slug)
        commit_sha: str | None = None
        artifacts: list[Artifact] = []
        pr_payload: dict[str, Any] = {
            "verdict": verdict,
            "reasoning": reasoning,
            "learning_slug": learning_slug,
        }

        if learning_path is not None:
            text = learning_path.read_text()
            fm, _ = _parse_frontmatter(text)
            check_count = int(fm.get("crystallization_check_count", "0"))
            _update_learning_not_ready(
                learning_path,
                reasoning=reasoning,
                check_count=check_count,
            )
            git.stage_all(ctx.repo_dir)
            if git.has_staged_changes(ctx.repo_dir):
                commit_sha = git.commit_all(
                    ctx.repo_dir,
                    _commit_message(ctx.ctx),
                    author_name=ctx.repo_config.git_author_name if ctx.repo_config else None,
                    author_email=ctx.repo_config.git_author_email if ctx.repo_config else None,
                    trailer=ctx.repo_config.commit_trailer if ctx.repo_config else None,
                )
                git.push_branch(ctx.repo_dir, ctx.branch)
                pr_number, pr_url = git.open_pr(
                    repo_dir=ctx.repo_dir,
                    branch=ctx.branch,
                    title=ctx.ctx.title,
                    body=summary,
                    repo=ctx.ctx.repo,
                    mode=ctx.settings.repo_mode,
                )
                artifacts = [Artifact(kind="branch", value=ctx.branch)]
                if pr_url:
                    artifacts.append(Artifact(kind="pr_url", value=pr_url))
                if pr_number is not None:
                    pr_payload["pr_number"] = pr_number
        else:
            logger.warning(
                "crystallization-judge: no learning file found for slug=%s",
                learning_slug,
            )

        return StepOutput(
            summary=summary,
            decision="not-ready",
            commit_sha=commit_sha,
            artifacts=artifacts,
            payload=pr_payload,
            metadata=Metadata(),
        )

    # verdict == "defer": no-op
    return StepOutput(
        summary=summary,
        decision="defer",
        commit_sha=None,
        artifacts=[Artifact(kind="analysis", value=summary)],
        payload={
            "verdict": verdict,
            "reasoning": reasoning,
            "learning_slug": learning_slug,
        },
        metadata=Metadata(),
    )


def _handle_crystallize(ctx: DispositionContext) -> StepOutput:
    """Step 2: write rule YAML + check.sh, update learning, commit + push + PR."""
    from treadmill_agent import git
    from treadmill_agent.runner import _commit_message  # local import avoids cycle

    summary = ctx.claude_result.summary or ""

    # Read slugs from the prior step (judge) payload.
    prior_payload: dict[str, Any] = {}
    if ctx.ctx.prior_steps:
        last_output = ctx.ctx.prior_steps[-1].output or {}
        prior_payload = last_output.get("payload") or {}

    learning_slug: str = (
        prior_payload.get("learning_slug")
        or prior_payload.get("dispatch", {}).get("learning_slug", "")
    )
    rule_slug: str = (
        prior_payload.get("proposed_rule_slug")
        or prior_payload.get("dispatch", {}).get("proposed_rule_slug", "")
    )

    # Extract rule YAML and check.sh from the architect's summary.
    rule_yaml = _extract_rule_yaml(summary)
    check_sh = _extract_check_sh(summary)

    # Derive rule_slug from the YAML name: field when not in prior payload.
    if not rule_slug and rule_yaml:
        rule_slug = _slug_from_yaml(rule_yaml)

    # Write docs/knowledge-base/rules/<slug>.yaml
    if rule_yaml and rule_slug:
        rules_dir = ctx.repo_dir / "docs" / "knowledge-base" / "rules"
        rules_dir.mkdir(parents=True, exist_ok=True)
        (rules_dir / f"{rule_slug}.yaml").write_text(rule_yaml + "\n")
        logger.info("crystallize: wrote rule YAML for %s", rule_slug)

    # Write tools/rule-checks/<slug>/check.sh
    if check_sh and rule_slug:
        checks_dir = ctx.repo_dir / "tools" / "rule-checks" / rule_slug
        checks_dir.mkdir(parents=True, exist_ok=True)
        check_sh_path = checks_dir / "check.sh"
        check_sh_path.write_text(check_sh + "\n")
        check_sh_path.chmod(0o755)
        logger.info("crystallize: wrote check.sh for %s", rule_slug)

    # Update source learning's status.
    if learning_slug and rule_slug:
        learning_path = _find_learning_file(ctx.repo_dir, learning_slug)
        if learning_path is not None:
            _update_learning_crystallized(learning_path, rule_slug=rule_slug)
        else:
            logger.warning(
                "crystallize: no learning file found for slug=%s", learning_slug
            )

    git.stage_all(ctx.repo_dir)
    commit_sha = git.commit_all(
        ctx.repo_dir,
        _commit_message(ctx.ctx),
        author_name=ctx.repo_config.git_author_name if ctx.repo_config else None,
        author_email=ctx.repo_config.git_author_email if ctx.repo_config else None,
        trailer=ctx.repo_config.commit_trailer if ctx.repo_config else None,
    )
    git.push_branch(ctx.repo_dir, ctx.branch)
    pr_number, pr_url = git.open_pr(
        repo_dir=ctx.repo_dir,
        branch=ctx.branch,
        title=ctx.ctx.title,
        body=summary,
        repo=ctx.ctx.repo,
        mode=ctx.settings.repo_mode,
    )

    artifacts: list[Artifact] = [Artifact(kind="branch", value=ctx.branch)]
    if pr_url:
        artifacts.append(Artifact(kind="pr_url", value=pr_url))
    payload: dict[str, Any] = {
        "rule_slug": rule_slug,
        "learning_slug": learning_slug,
    }
    if pr_number is not None:
        payload["pr_number"] = pr_number

    return StepOutput(
        summary=summary,
        decision="pushed",
        commit_sha=commit_sha,
        artifacts=artifacts,
        payload=payload,
        metadata=Metadata(),
    )


def handle(ctx: DispositionContext) -> StepOutput:
    """Dispatch to the judge or crystallize step based on role.id."""
    if ctx.ctx.role.id == "role-crystallization-judge":
        return _handle_judge(ctx)
    return _handle_crystallize(ctx)
