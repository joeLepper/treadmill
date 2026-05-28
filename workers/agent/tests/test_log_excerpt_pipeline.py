"""ADR-0058 Step 4 — pin the log_excerpt → architect prompt data flow.

Background. The 2026-05-27 RAMJAC incident exposed a ralph-loop deadlock
where the author's deterministic gate kept failing with
``ModuleNotFoundError: No module named 'aws_cdk'`` — the worker sandbox
lacked the tooling the gate required. The architect's gate-broken
verdict (ADR-0058) is the escape valve, but it depends on the
deterministic gate's stderr reaching the architect's prompt verbatim:
the architect needs to *cite* the failing tooling's output in
``gate_log_excerpt``. The incident proved ``log_excerpt`` reaches the
worker's ``StepOutput`` (it gets embedded into ``summary`` by
``code._compose_validation_failure_summary``); what was NOT yet pinned
is that it survives the next hop — the architect step's prompt
composition — intact.

This test pins the load-bearing seam. If a future refactor truncates or
drops ``log_excerpt`` anywhere between
``run_deterministic`` → ``_compose_validation_failure_summary`` →
``StepOutput.summary`` → ``_render_prior_step_block`` → architect prompt,
this test fails loudly so the regression surfaces at PR review rather
than as a silent gate-broken-blind architect.
"""

from __future__ import annotations

from treadmill_agent import claude_code
from treadmill_agent.api_client import PriorStep, Role
from treadmill_agent.runner_dispositions.code import (
    _compose_validation_failure_summary,
)
from treadmill_agent.validation_runtime import CheckResult


# Canonical 2026-05-27 case: the author's deterministic gate ran
# ``cdk synth`` (or equivalent) and the worker sandbox crashed at import
# time because ``aws_cdk`` isn't installed. The stderr the architect must
# see verbatim to verdict gate-broken with confidence.
_AWS_CDK_MODULE_NOT_FOUND_STDERR = (
    "--- stderr ---\n"
    "Traceback (most recent call last):\n"
    '  File "/var/treadmill/workspaces/abc/repo/app.py", line 3, '
    "in <module>\n"
    "    import aws_cdk\n"
    "ModuleNotFoundError: No module named 'aws_cdk'\n"
)


def _architect_role() -> Role:
    """A minimal Role stub for ``role-architect`` — only the fields
    ``_compose_prompt`` actually reads need to be populated.

    The architect's real role record carries skills + a system_prompt
    that primes the gate-broken classification; neither matters for
    pinning the data-flow seam this test is about.
    """
    return Role(
        id="role-architect",
        model="claude-opus-4-7",
        system_prompt="you are the treadmill architect",
        output_kind="analysis",
        skills=[],
        hooks=[],
    )


def test_step_output_log_excerpt_reaches_architect_prompt() -> None:
    """The deterministic-gate stderr ``log_excerpt`` must appear
    verbatim in the architect's composed prompt input.

    Walks the production path:

      1. ``run_deterministic`` produces a ``CheckResult`` whose
         ``log_excerpt`` carries the gate's stderr (the canonical
         2026-05-27 ``ModuleNotFoundError: No module named 'aws_cdk'``).
      2. The code disposition's failure path calls
         ``_compose_validation_failure_summary`` which embeds
         ``log_excerpt`` into the human-readable ``StepOutput.summary``.
      3. The architect is dispatched with that step as its
         ``prior_steps[-1]``; the runner's ``_compose_prompt`` (via
         ``_render_prior_step_block``) surfaces the prior step's
         ``output.summary`` into the architect's prompt.

    If any link drops or truncates the excerpt — including a future
    refactor that swaps the summary composer, narrows what
    ``_render_prior_step_block`` surfaces, or replaces the prior-step
    folding with a ``payload``-only projection — this test fails. The
    architect can't return a credible ``gate-broken`` verdict without
    the original stderr (the ``gate_log_excerpt`` field's contract per
    ADR-0058 requires the failing tooling's evidence verbatim).
    """
    # Step 1: the deterministic gate runs and surfaces the failing
    # tooling's stderr in the CheckResult envelope.
    check_result = CheckResult(
        check_id="cdk-synth",
        kind="deterministic",
        severity="blocking",
        verdict="fail",
        rationale="Script exited 1: cdk synth",
        log_excerpt=_AWS_CDK_MODULE_NOT_FOUND_STDERR,
    )

    # Step 2: code disposition's failure path embeds the excerpt into
    # the StepOutput.summary text (the load-bearing summary composer).
    summary = _compose_validation_failure_summary([check_result])
    assert "ModuleNotFoundError: No module named 'aws_cdk'" in summary, (
        "_compose_validation_failure_summary must embed log_excerpt "
        "into the summary so the architect can read it via the prior-"
        "step block. Regression here is the first place the data flow "
        "breaks."
    )

    # Step 3: the source step output as the architect would see it on
    # ``prior_steps[-1].output`` — a dict shape mirroring the
    # serialized ``StepOutput`` envelope (ADR-0012).
    source_step_output = {
        "summary": summary,
        "decision": "fail",
        "commit_sha": None,
        "artifacts": [],
        "payload": {
            "validation_results": [
                {
                    "check_id": check_result.check_id,
                    "kind": check_result.kind,
                    "verdict": check_result.verdict,
                    "rationale": check_result.rationale,
                    "log_excerpt": check_result.log_excerpt,
                },
            ],
        },
    }
    prior = PriorStep(
        step_index=0,
        step_name="author",
        role_id="role-code-author",
        status="completed",
        output=source_step_output,
    )

    # Step 4: drive the architect's context-injection helper —
    # ``_compose_prompt`` folds ``prior_steps[-1]`` via
    # ``_render_prior_step_block``. This is the seam the architect's
    # gate-broken verdict depends on.
    prompt = claude_code._compose_prompt(
        role=_architect_role(),
        task_title="Resolve gate failure for task <id>",
        task_description="The author hit a deterministic gate failure.",
        plan_intent=None,
        prior_steps=[prior],
    )

    # The full ModuleNotFoundError string must round-trip verbatim into
    # the architect's prompt — character-for-character — so the verdict
    # can quote it as evidence in ``gate_log_excerpt``.
    assert "ModuleNotFoundError: No module named 'aws_cdk'" in prompt, (
        "log_excerpt was lost between StepOutput.summary and the "
        "architect's composed prompt. Per ADR-0058, the architect must "
        "see the deterministic gate's stderr verbatim to verdict "
        "gate-broken with the required ``gate_log_excerpt`` evidence. "
        "Check _render_prior_step_block and _compose_prompt for a "
        "summary-projection regression."
    )
