"""Per-kind dispatch handlers for the worker runner (ADR-0022).

The runner's ``_execute`` runs Claude Code via a shared prefix (clone,
checkout, drive the LLM, stream output), then dispatches to one of
five kind-specific handlers based on the role's ``output_kind``:

  * ``code``          → ``code.handle``          — diff/commit/push/PR
  * ``review``        → ``review.handle``         — post ``gh pr review``
  * ``analysis``      → ``analysis.handle``       — emit artifact, no side effects
  * ``plan_doc``      → ``plan_doc.handle``       — like code, diff confined to docs/plans/
  * ``documentation`` → ``documentation.handle``  — amend doc artifacts + Class C escalation

Each handler accepts a ``DispositionContext`` (the runner's per-step
state) plus the ``CodeAuthorResult`` from Claude Code and the path to
the working tree, and returns a uniform ``StepOutput`` envelope per
ADR-0012.

Empty-diff semantics differ by kind:

  * ``code``          — empty diff is failure (the role was asked to make
                        changes and didn't).
  * ``review``        — empty diff is success (review is the side effect).
  * ``analysis``      — empty diff is success (output is the artifact).
  * ``plan_doc``      — empty diff is failure (the role was asked to author
                        a plan doc and didn't).
  * ``documentation`` — empty diff is failure (the role was asked to amend
                        a doc artifact and didn't).

The dispatch table is in ``runner.py`` itself — keeping it there
avoids a circular import (the handlers import from the runner's
context types; the table imports the handlers).
"""

from __future__ import annotations

from treadmill_agent.runner_dispositions.analysis import handle as handle_analysis
from treadmill_agent.runner_dispositions.architecture import handle as handle_architecture
from treadmill_agent.runner_dispositions.code import handle as handle_code
from treadmill_agent.runner_dispositions.crystallization import handle as handle_crystallization
from treadmill_agent.runner_dispositions.documentation import handle as handle_documentation
from treadmill_agent.runner_dispositions.plan_doc import handle as handle_plan_doc
from treadmill_agent.runner_dispositions.review import handle as handle_review
from treadmill_agent.runner_dispositions.validation import handle as handle_validation

__all__ = [
    "handle_analysis",
    "handle_architecture",
    "handle_code",
    "handle_crystallization",
    "handle_documentation",
    "handle_plan_doc",
    "handle_review",
    "handle_validation",
]
