"""Plan-doc parser: extract the strict-YAML ``## sequence_of_work`` block
from a plan markdown document, parse it, and validate against the
schemas declared in the Phase 2 plan.

Schema (from `docs/plans/2026-05-08-minimum-runnable-treadmill.md`):

  sequence_of_work:
    - id: <kebab-case slug>           # required, unique within plan
      title: <terse imperative>       # required
      workflow: <workflow slug>        # required
      depends_on: [...]                # optional
      intent: |                        # required
        Multi-line description.
      scope:
        files: [...]                   # required, at least one
        services_affected: [...]       # optional
        out_of_scope: [...]            # optional
      validation:                      # required, at least one entry
        - kind: deterministic | llm-judge
          description: ...

Strict validation: ``extra="forbid"`` rejects unknown fields. Required
fields raise on absence. The parser does not guess — malformed docs fail
loudly with paths to the offending field.
"""

from __future__ import annotations

import re
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class PlanDocFormatError(ValueError):
    """Raised when a plan doc does not contain a parseable sequence_of_work."""


# ── Pydantic schemas (strict) ─────────────────────────────────────────────────


class TaskScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: list[str] = Field(
        ...,
        min_length=1,
        description="Files the task is allowed to modify or create.",
    )
    services_affected: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)


class TaskValidationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["deterministic", "llm-judge"]
    description: str
    # ``script`` is added in Phase 4 alongside the rule engine.


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Kebab-case slug, unique within the plan.")
    title: str
    workflow: str = Field(..., description="Workflow slug; must reference a known workflow.")
    depends_on: list[str] = Field(default_factory=list)
    intent: str
    scope: TaskScope
    validation: list[TaskValidationCheck] = Field(..., min_length=1)


class _PlanDocSequenceContainer(BaseModel):
    """Top-level shape of the YAML block: ``sequence_of_work: [TaskSpec, ...]``.

    Internal model — callers receive ``list[TaskSpec]`` directly from
    ``parse_plan_doc``."""

    model_config = ConfigDict(extra="forbid")
    sequence_of_work: list[TaskSpec] = Field(..., min_length=1)


# ── Parser ────────────────────────────────────────────────────────────────────


_HEADING_RE = re.compile(r"^##\s+sequence[_\s]of[_\s]work\s*$", re.MULTILINE | re.IGNORECASE)
_YAML_BLOCK_RE = re.compile(r"```ya?ml\s*\n(.*?)\n```", re.DOTALL)


def extract_sequence_yaml(markdown_text: str) -> str:
    """Return the raw YAML body of the ``## sequence_of_work`` block.

    Looks for the first ``## sequence_of_work`` markdown heading, then the
    next ``\`\`\`yaml`` (or ``\`\`\`yml``) fenced block following it. Returns the
    YAML body without the fences.

    Raises:
        PlanDocFormatError: if the heading or the fenced block is missing.
    """
    heading = _HEADING_RE.search(markdown_text)
    if heading is None:
        raise PlanDocFormatError(
            "plan doc does not contain a '## sequence_of_work' heading"
        )
    after = markdown_text[heading.end():]
    block = _YAML_BLOCK_RE.search(after)
    if block is None:
        raise PlanDocFormatError(
            "no ```yaml or ```yml fenced block follows the "
            "'## sequence_of_work' heading"
        )
    return block.group(1)


def parse_plan_doc(markdown_text: str) -> list[TaskSpec]:
    """Extract, parse, and validate the task specs from a plan doc.

    Returns the list of TaskSpec objects in the order they appeared.

    Raises:
        PlanDocFormatError: if the heading / fenced block is missing or
            the YAML cannot be parsed at all.
        pydantic.ValidationError: if the YAML parses but does not conform
            to the strict TaskSpec schema (missing fields, extras, etc.).
    """
    yaml_text = extract_sequence_yaml(markdown_text)
    try:
        raw = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise PlanDocFormatError(f"sequence_of_work YAML failed to parse: {exc}") from exc

    if not isinstance(raw, dict):
        raise PlanDocFormatError(
            "sequence_of_work YAML body must be a mapping with a "
            "'sequence_of_work' key, not a list or scalar"
        )

    container = _PlanDocSequenceContainer.model_validate(raw)
    return container.sequence_of_work


def validate_unique_task_ids(specs: list[TaskSpec]) -> None:
    """Reject plans whose task IDs are not unique within the plan."""
    seen: set[str] = set()
    for spec in specs:
        if spec.id in seen:
            raise PlanDocFormatError(f"duplicate task id within plan: {spec.id!r}")
        seen.add(spec.id)
