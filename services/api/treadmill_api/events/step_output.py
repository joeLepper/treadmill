"""Uniform StepOutput envelope (ADR-0012).

Every role, every workflow, every step writes a ``StepOutput``. The
envelope replaces the Week-2-closure ``AuthorStepOutput | dict[str, object]``
union with a single Pydantic model whose top-level shape is universal
and whose ``payload`` field is the per-workflow polymorphic surface
(validated by consumer convention, not statically typed at the
boundary).

Per ADR-0012 §"Why each top-level field is where it is":

* ``summary``       - required human-readable headline.
* ``decision``      - required free string the consumer matches by value;
                      per-workflow value-sets are documented in ADR-0012
                      §"Decision-string value-sets per workflow".
* ``commit_sha``    - universal anchor for when the step ran; required
                      at top-level (not in ``payload``) because the
                      mergeability VIEW (ADR-0013) LATERAL-joins on
                      ``output->>'commit_sha'``.
* ``artifacts``     - typed-kind references (PR URLs, branches, comment
                      IDs, file paths, doc paths, log URIs). ``kind`` is
                      a strict ``Literal[...]`` — extending it is a
                      one-line schema change + ADR amendment.
* ``payload``       - workflow-specific extras. Validated by consumer
                      convention. The implicit discriminator is the run's
                      ``workflow_id``.
* ``metadata``      - operational fields (model, token counts, cost,
                      duration). Audience is operators, not consumers
                      of workflow content.

The ``extra="forbid"`` config on every model in this file enforces no
unknown top-level keys at validation time. ``payload``'s ``dict[str, Any]``
is the explicit escape hatch for per-workflow conventions.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class Artifact(BaseModel):
    """A typed reference produced by a step.

    ``kind`` is a strict ``Literal`` so the consumer can match on a
    closed set without string-typo surprises. ``value`` is the canonical
    identifier for the kind (URL for ``pr_url``, branch name for
    ``branch``, etc.). ``label`` is optional human-readable text — the
    future UI uses it when present and falls back to ``value`` otherwise.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "pr_url",
        "branch",
        "commit_sha",
        "comment_id",
        "file_path",
        "doc_path",
        "log_uri",
        # ADR-0022 per-kind dispatch handler outputs.
        "analysis",   # text emitted by an ``analysis``-kind role
        "pr_review",  # the verdict from a ``review``-kind role
    ]
    value: str
    label: str | None = None


class Metadata(BaseModel):
    """Operational metadata for a step's execution.

    All fields are optional because not every step type has them — a
    non-LLM step has no tokens; a synchronous role has no duration in
    the LLM-call sense. ``extra`` is the operational escape hatch
    (model temperature, retry count, claude-code session id, etc.).
    Per ADR-0012 it is *not* a JSONB dumping ground for workflow content;
    workflow-specific data lives in ``StepOutput.payload``.
    """

    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    extra: dict[str, Any] = {}


class StepOutput(BaseModel):
    """The uniform step output envelope (ADR-0012).

    This is the type of both ``WorkflowRunStep.output`` (the persisted
    JSONB shape) and ``StepCompleted.output`` (the wire shape the worker
    publishes). Every role, every workflow, every step writes it.

    ``summary`` and ``decision`` are required; everything else has a
    sensible default. ``extra="forbid"`` rejects unknown top-level keys —
    the discipline that catches malformed envelopes at the boundary.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str
    decision: str
    commit_sha: str | None = None
    artifacts: list[Artifact] = []
    payload: dict[str, Any] = {}
    metadata: Metadata = Metadata()
