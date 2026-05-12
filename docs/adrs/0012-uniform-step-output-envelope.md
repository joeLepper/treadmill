# ADR-0012: Uniform StepOutput envelope

- **Status:** accepted
- **Date:** 2026-05-11
- **Related:** ADR-0010, ADR-0011, ADR-0013, ADR-0015, learning:2026-05-11-uniform-output-shape-over-per-workflow-typing

## Context

Treadmill's workflows (per ADR-0010) emit step-completion events whose `output` field carries the result of the role's execution. The Week-2 closure plan landed `StepCompleted.output: AuthorStepOutput | dict[str, object]` (closure plan A.4) — a typed-for-author / fallback-for-other-step-types union. That shape was the orchestrator's first reach for "type safety at the boundary," and the closure plan called for promoting per-workflow typed outputs as each new workflow shipped (`ReviewStepOutput`, `ValidateStepOutput`, etc.).

A 2026-05-11 design exchange surfaced that this is the wrong direction. Bunkhouse went through the per-role schema phase and reached a different answer: a **uniform envelope** that every role and every workflow conforms to. The motivations are concrete:

- A future UI renders step outputs and benefits from a single render path that handles every workflow, not per-workflow render code.
- Cross-workflow analytics / search / aggregation are dramatically cheaper when every step has the same headline fields.
- Evolution lands cleaner — new workflows do not require schema migrations on the consumer side.

Per `learning:2026-05-11-uniform-output-shape-over-per-workflow-typing`, the orchestrator's bias toward compile-time validation of payload contents was the failure mode. Bunkhouse's precedent settled this question; Treadmill adopts the posture from day one.

This ADR is short because the architectural call is already settled by the learning. It records *what* and is terse about *why*.

## Decision

### The envelope

A single Pydantic `StepOutput` model is the type of `WorkflowRunStepStep.output` and of `StepCompleted.output`. Every role, every workflow, every step writes it. New file: `services/api/treadmill_api/events/step_output.py`.

```python
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal[
        "pr_url", "branch", "commit_sha",
        "comment_id", "file_path", "doc_path", "log_uri",
    ]
    value: str
    label: str | None = None  # optional human-readable description


class Metadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    extra: dict[str, Any] = {}


class StepOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str
    decision: str
    commit_sha: str | None = None
    artifacts: list[Artifact] = []
    payload: dict[str, Any] = {}
    metadata: Metadata = Metadata()
```

`summary` and `decision` are required; everything else has a sensible default. `extra="forbid"` enforces no unknown top-level keys. `payload` is the explicit polymorphic surface.

### Why each top-level field is where it is

- **`summary: str`** — the human-readable headline. Required. Bunkhouse's experience without this field was that the UI ends up rendering the first line of `logs[0]` or some equally fragile thing. A required summary is one line of role discipline that pays for itself the first time we ship a UI.

- **`decision: str`** — a free string the consumer matches by value. Workflow-specific value-sets are documented (see below) but not statically typed at the envelope level. Per the learning: convention, not types. A future lint rule can validate post-hoc that `wf-author`'s decision is in `{pushed, blocked, no-changes}`.

- **`commit_sha: str | None`** — the universal anchor for "when did this step run." Top-level, not in `payload`. Three reasons: ADR-0013's `task_mergeability` VIEW LATERAL-joins `output->>'commit_sha' = head_sha` and a clean top-level field is operationally cheaper than a JSON dig through `payload`; `commit_sha` is the *only* per-execution field that's universally meaningful across every workflow that runs against a PR (it is not per-workflow-specific, which is what `payload` is for); demoting it to `payload` would make the convention fragile precisely where ADR-0013 depends on its stability. Nullable because some workflows (`wf-plan`) run before any commit exists.

- **`artifacts: list[Artifact]`** — typed-kind references the step produced. PR URLs, branches, comment IDs, files, doc paths, log URIs. `kind` is a strict `Literal[...]` for v0; adding a kind is a one-line schema change + an ADR amendment. The strict-mode discipline mirrors ADR-0011's posture on JSONB at the boundary.

- **`payload: dict[str, Any]`** — workflow-specific extras. Validated by consumer convention, not statically typed at the boundary. The implicit discriminator is the run's workflow_id; the consumer knows what to expect there per documented per-workflow conventions.

- **`metadata: Metadata`** — operational fields. Model, token counts, cost, duration. All optional because not every step type has them (a non-LLM step has no tokens). `metadata.extra` is the operational escape hatch — model temperature, retry count, claude-code session id, etc. — *not* a JSONB dumping ground; the audience is operators, not consumers of workflow content.

### Decision-string value-sets per workflow

Documented here; not statically validated. ADR-0015 maintains this matrix as workflows + roles evolve.

| Workflow | Step | Decision values |
|---|---|---|
| `wf-author` | author | `pushed` / `blocked` / `no-changes` |
| `wf-plan` | research (analyzer) | `plan-ready` / `blocked` |
| `wf-plan` | plan-author (action) | `plan-doc-pushed` / `blocked` |
| `wf-review` | review | `approved` / `changes_requested` / `needs-more-info` |
| `wf-validate` | validate | `pass` / `fail` / `error` |
| `wf-feedback` | analyzer | `plan-ready` / `no-action-needed` / `blocked` |
| `wf-feedback` | action | `code-change-dispatched` / `responded-without-change` / `blocked` |
| `wf-ci-fix` | analyzer | `plan-ready` / `not-our-bug` / `blocked` |
| `wf-ci-fix` | action | `fix-pushed` / `gave-up` / `not-our-bug` |
| `wf-conflict` | analyzer | `plan-ready` / `blocked` |
| `wf-conflict` | action | `resolved` / `gave-up` |

### Convention map for `wf-author`'s `payload`

The Week-2 closure landed `AuthorStepOutput` with fields `branch`, `pr_number`, `pr_url`, `commit_sha`, `summary`. With the envelope:

- `summary` → top-level `summary`
- `commit_sha` → top-level `commit_sha`
- `branch` → `Artifact(kind="branch", value="task/...")`
- `pr_url` → `Artifact(kind="pr_url", value="https://...")`
- `pr_number` → `payload["pr_number"]: int` (convention)

`AuthorStepOutput` as a Pydantic class is **removed** from `events/step.py`. Its fields become a documented convention for what `wf-author` writes into the envelope. Future workflows extend the convention table per their needs.

The consumer's existing `_write_task_prs_on_completed` (from closure plan B.8) reads:
- `pr_number` from `payload`
- `branch` from the artifact list (`kind="branch"`)
- `commit_sha` from top-level

### Migration

One-pass rewrite. v0 has no production data. Worker (`workers/agent/treadmill_agent/runner.py:_execute` and `eventbus.py:_publish`) constructs `StepOutput(...)` instead of `AuthorStepOutput(...)`. Consumer's `step.completed` branch reads the envelope. `events/step.py:StepCompleted` becomes `output: StepOutput`. Tests update in lockstep. No alembic migration — the column was JSONB throughout; only the Pydantic shape changes.

## Bunkhouse precedent

Bunkhouse's actual implementation reads only three convention fields from step output: `pr_number`, `branch`, `logs` (verified 2026-05-11 via targeted lookup at `bunkhouse/services/api/bunkhouse/events/consumer.py:475-594`). Bunkhouse has *no envelope at all* — the output is a JSON dict with conventional fields. Treadmill's envelope is **stricter** than bunkhouse's posture: we add `summary` + `decision` + `commit_sha` + structured `artifacts` + structured `metadata`, validated via Pydantic at the boundary per ADR-0011. The stricter shape is justified because (a) ADR-0013's mergeability VIEW depends on `commit_sha` at top-level, (b) the future UI surface ADR-0001 hints at benefits from `summary` + `decision`, (c) `logs` lives in `metadata.extra.logs` or as `Artifact(kind="log_uri", ...)` per the per-step pattern.

The convention surface bunkhouse uses is fully covered by Treadmill's envelope. No bunkhouse-read field is left without a home.

## Trade-offs

- **No compile-time validation of `payload` contents.** Consumers must know per-workflow conventions or be defensive. Mitigation: per-workflow conventions are documented in this ADR + ADR-0015; a future lint rule (`rule:step-payload-conforms-to-workflow`) evaluates post-hoc.
- **Boundary discipline is non-trivial.** `extra="forbid"` at envelope level catches malformed top-level fields; `payload`'s dict[str, Any] does not. The defensive write-raw-dict pattern from the Week-2 closure (decision #2) stays — if a typed payload convention fails to validate, the consumer writes raw and logs.
- **Free-string `decision` loses the "did we typo this enum?" guarantee.** Accepted; the lint rule replaces it.
- **`commit_sha` at top-level is a Treadmill addition vs. bunkhouse.** Documented explicitly because ADR-0013's VIEW depends on the placement.

## Alternatives considered

- **Per-workflow typed outputs** (the closure plan's A.4 trajectory: `ReviewStepOutput`, `ValidateStepOutput`, etc.). Rejected per `learning:2026-05-11-uniform-output-shape-over-per-workflow-typing`. The compile-time-validation instinct is the named failure mode there.
- **`commit_sha` in `payload`** (the user's original envelope proposal). Rejected after researcher push-back. The mergeability VIEW depends on a clean top-level join; `commit_sha` is universal-not-per-workflow.
- **`commit_sha` as an `Artifact(kind="commit_sha", ...)`**. Rejected. Artifacts are a *list* of references; the cardinality is wrong for an *anchoring* field. Mixing anchors with references makes the artifact kind enum messier without payoff.
- **No `summary` field; let the UI derive a headline.** Rejected. Bunkhouse's experience proves the UI ends up showing fragile fallbacks. A required summary is cheap role discipline.
- **Strict `decision: Literal[...]` union per workflow.** Rejected per the learning — convention beats types here. Post-hoc lint is the alternative.
- **Open-string `Artifact.kind`.** Rejected for v0. Strict enum is cheaper and the seven kinds cover today's needs. Adding a kind is a one-line schema change + amendment.
- **Stricter `metadata` (every field required).** Rejected — not every step type has token counts; all-optional with `extra` for one-offs is the right shape.
- **Promote `Artifact.value` to a structured object per kind** (e.g. `pr_url` having both URL and number fields). Rejected for v0 — `value` stays a string; convention is "the value is the canonical identifier for the kind." `pr_number` lives in `payload` rather than splitting the PR reference across two artifact fields.

## Consequences

- ADR-0013 builds on this envelope's `commit_sha` field.
- ADR-0015 builds on this envelope's `payload` convention for the `task_directive` analyzer→action contract.
- A future lint rule (`rule:step-payload-conforms-to-workflow`) will validate convention adherence post-hoc.
- The Week-3 plan doc (`docs/plans/2026-05-12-week-3-mergeable-and-multi-step.md`) sequences the worker + consumer rewrite as the first item after this ADR lands.
