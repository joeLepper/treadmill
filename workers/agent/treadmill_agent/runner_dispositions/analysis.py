"""``analysis`` disposition — emit the artifact, no PR-side side effects.

Per ADR-0022, an analysis-kind role produces text consumed by a
downstream step in the same workflow run (the analyzer→action contract
per ADR-0015). The handler returns a ``StepOutput`` whose
``artifacts`` carries the summary as an ``Artifact(kind="analysis",
...)``; the downstream step reads the upstream step's output via the
existing step-output composition.

Empty diff is a SUCCESS. The role wasn't asked to modify code.

The handler also surfaces a ``task_directive`` synthesized from the
summary into ``payload.task_directive`` when the prior-step handoff
needs structured data. In production, the analyzer role's prompt
instructs Claude to emit a JSON task_directive inline; the parsing of
that is a future ADR. For v0, the handler passes Claude's summary
through verbatim — the downstream role's prompt-composer surfaces the
analyzer's full summary so the action role has the context it needs.
"""

from __future__ import annotations

from typing import Any

from treadmill_agent.events import Artifact, Metadata, StepOutput
from treadmill_agent.runner_dispositions._context import DispositionContext


def handle(ctx: DispositionContext) -> StepOutput:
    """Wrap the Claude summary in the envelope and return.

    No git side effects (no stage, commit, push); no PR-side side
    effects (no gh CLI calls). The artifact is the analyzer's
    handoff to the downstream action step.
    """
    payload: dict[str, Any] = {}
    # Dry-run analyzer extension (ADR-0015 §D.1): synthesize a minimal
    # ``task_directive`` so the downstream action step's prior_steps
    # composition has something to read. Production analyzers will
    # eventually emit the directive themselves; the dry-run path keeps
    # the cross-step handoff exercisable end-to-end.
    if ctx.is_dry_run:
        from treadmill_agent.runner import _dry_run_task_directive  # local import

        payload["task_directive"] = _dry_run_task_directive(ctx.ctx)
    return StepOutput(
        summary=ctx.claude_result.summary,
        # ADR-0015's analyzer→action contract uses ``plan-ready`` as
        # the default "directive attached" decision. Per-role prompts
        # may override (e.g. ``no-action-needed``, ``not-our-bug``);
        # at v0 we don't try to parse those out of free-form text.
        decision="plan-ready",
        commit_sha=None,
        artifacts=[Artifact(kind="analysis", value=ctx.claude_result.summary)],
        payload=payload,
        metadata=Metadata(),
    )
