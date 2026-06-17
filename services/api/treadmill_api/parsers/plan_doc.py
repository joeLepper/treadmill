"""Plan-doc parser: extract the strict-YAML ``## sequence_of_work`` block
from a plan markdown document, parse it, and validate against the
schemas declared in the Phase 2 plan.

Schema (from `docs/plans/2026-05-08-minimum-runnable-treadmill.md`,
amended post-ADR-0087 Phase 5 — task 56c0b353):

  sequence_of_work:
    - id: <kebab-case slug>           # required, unique within plan
      title: <terse imperative>       # required
      workflow: <workflow slug>        # OPTIONAL, deprecated: accepted-and-ignored
      depends_on: [...]                # optional
      intent: |                        # required
        Multi-line description.
      scope:
        files: [...]                   # required, at least one
        services_affected: [...]       # optional
        out_of_scope: [...]            # optional
      validation:                      # OPTIONAL, deprecated: accepted-and-ignored
        - kind: deterministic | llm-judge
          description: ...

Strict validation: ``extra="forbid"`` rejects unknown fields. Required
fields raise on absence. The parser does not guess — malformed docs fail
loudly with paths to the offending field.

``workflow:`` and ``validation:`` became optional with ADR-0087 Phases
4/5: workflow versions and per-task validation gates were deleted, both
fields are inert at submit (``_spawn_tasks_from_specs`` never reads
them), and requiring them forced submitters to include dead fields just
to pass parsing (the ramjac #1329 workaround). Present values are
still shape-validated and then ignored, so older docs parse unchanged.
"""

from __future__ import annotations

import re
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)


class PlanDocFormatError(ValueError):
    """Raised when a plan doc does not contain a parseable sequence_of_work."""


# ── Frontmatter schema (ADR-0031 §Decision Q31.c) ────────────────────────────


class PlanFrontmatter(BaseModel):
    """Optional YAML frontmatter block at the top of a plan markdown doc.

    Currently a single field — ``auto_merge`` — controlling whether the
    plan's PRs are eligible for the ADR-0031 auto-merge cooling-off
    trigger. ``None`` (omitted) and ``True`` both mean enabled; ``False``
    is an explicit opt-out.

    ``StrictBool`` rejects string-coerced values like ``"true"`` /
    ``"false"`` / ``"1"`` so a plan author who quoted the value by
    accident fails loudly instead of silently opting in or out. Note:
    bare YAML ``true`` / ``false`` / ``yes`` / ``no`` / ``on`` / ``off``
    are parsed to real Python booleans by PyYAML before pydantic sees
    them, so they're accepted (correctly).

    Tolerates unrelated fields (``status``, ``trigger``, ``parent``, ...)
    because plan-doc frontmatter is conventionally used for several
    things beyond auto-merge. Typos in ``auto_merge`` therefore silently
    default to enabled — a deliberate trade-off until we either model
    the full frontmatter shape or grow a separate auto-merge-specific
    key path.
    """

    model_config = ConfigDict(extra="ignore")

    auto_merge: StrictBool | None = None


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
    script: str | None = None
    prompt: str | None = None
    severity: Literal["blocking", "warning", "advisory"] = "blocking"
    llm_model: str | None = None
    timeout_seconds: int = 30

    @model_validator(mode='after')
    def _validate_kind_content(self) -> "TaskValidationCheck":
        if self.kind == "deterministic":
            if not self.script:
                raise ValueError("deterministic requires script")
            if self.prompt:
                raise ValueError("deterministic forbids prompt")
        else:  # llm-judge
            if not self.prompt:
                raise ValueError("llm-judge requires prompt")
            if self.script:
                raise ValueError("llm-judge forbids script")
        return self


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Kebab-case slug, unique within the plan.")
    title: str
    # DEPRECATED (ADR-0087 Phase 5): workflow versions are gone; the
    # coordinator decides execution at dispatch time. Accepted for
    # back-compat (older docs carry ``workflow: wf-author``) and ignored.
    workflow: str | None = Field(
        default=None,
        description="DEPRECATED — accepted-and-ignored post-ADR-0087 Phase 5.",
    )
    depends_on: list[str] = Field(default_factory=list)
    intent: str
    scope: TaskScope
    # DEPRECATED (ADR-0087 Phases 4/5): per-task validation gates are
    # gone (the evaluator's holistic judgment replaced them); blocks
    # still flow to workers via the coordinator's brief but are never
    # persisted. Present values stay shape-validated (a malformed check
    # is an authoring error worth failing on), and an EXPLICIT empty
    # list is still rejected — with a message that says to omit the key,
    # NOT pydantic's generic min-length text, which would steer authors
    # toward adding a dead entry (PR #327 review).
    validation: list[TaskValidationCheck] | None = Field(
        default=None,
        description="DEPRECATED — accepted-and-ignored post-ADR-0087 Phases 4/5.",
    )

    @field_validator("validation")
    @classmethod
    def _empty_validation_means_omit(
        cls, v: list[TaskValidationCheck] | None,
    ) -> list[TaskValidationCheck] | None:
        if v is not None and len(v) == 0:
            raise ValueError(
                "'validation: []' — omit the deprecated 'validation:' key "
                "entirely instead of passing an empty list (the field is "
                "accepted-and-ignored when present; per-task validation "
                "gates were removed in ADR-0087 Phases 4/5)"
            )
        return v


class _PlanDocSequenceContainer(BaseModel):
    """Top-level shape of the YAML block: ``sequence_of_work: [TaskSpec, ...]``.

    Internal model — callers receive ``list[TaskSpec]`` directly from
    ``parse_plan_doc``."""

    model_config = ConfigDict(extra="forbid")
    sequence_of_work: list[TaskSpec] = Field(..., min_length=1)


# ── Parser ────────────────────────────────────────────────────────────────────


_HEADING_RE = re.compile(r"^##\s+sequence[_\s]of[_\s]work\s*$", re.MULTILINE | re.IGNORECASE)
_YAML_BLOCK_RE = re.compile(r"```ya?ml\s*\n(.*?)\n```", re.DOTALL)
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)


def parse_plan_doc_frontmatter(markdown_text: str) -> PlanFrontmatter:
    """Parse the optional ``---``-delimited YAML frontmatter at the top
    of a plan doc.

    Returns a ``PlanFrontmatter`` whose fields are ``None`` when absent.
    If there is no frontmatter block, returns an empty ``PlanFrontmatter``.

    Raises:
        PlanDocFormatError: if the frontmatter delimiters are present but
            the YAML body fails to parse or is not a mapping.
        pydantic.ValidationError: if the parsed mapping contains unknown
            keys or values that violate the strict types.
    """
    m = _FRONTMATTER_RE.match(markdown_text)
    if m is None:
        return PlanFrontmatter()
    try:
        raw = yaml.safe_load(m.group(1))
    except yaml.YAMLError as exc:
        raise PlanDocFormatError(
            f"plan frontmatter YAML failed to parse: {exc}"
        ) from exc
    if raw is None:
        return PlanFrontmatter()
    if not isinstance(raw, dict):
        raise PlanDocFormatError(
            "plan frontmatter must be a YAML mapping; "
            f"got {type(raw).__name__}"
        )
    return PlanFrontmatter.model_validate(raw)


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
