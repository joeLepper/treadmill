---
date: 2026-05-27
trigger: incident
status: crystallized-into-rule-deterministic-gates-must-use-sandbox-safe-tools
related: ADR-0029, 2026-05-22-docker-restart-reuses-old-image-silent-noop-deploy
---
# Learning: Deterministic gates must run cleanly in the worker sandbox, or the architect-amend loop becomes a deadlock

## Trigger

Five RAMJAC tasks (`b6f8ca42`, `1effb8bf`, `e307b795`, `9a88451c`,
`795deb93`) wedged at `wf-architecture-resolve: failed` or `wf-author:
failed` despite the architect repeatedly verdicting `amend` and the
author repeatedly producing logically-complete code. Each task hit the
ADR-0029 cap (≥5 architect amends) and escalated to operator.

The architect's most recent amend rationale (task `b6f8ca42`) named the
pattern explicitly:

> The task is **Trigger B (ralph-loop deadlock)**. The task spec
> explicitly requires four deliverables: (1) `comprehendmedical:DetectEntitiesV2 +
> InferICD10CM` IAM grants on the DED task role in infra.py, ...

The architect knew it was a deadlock — but the only verdicts available
to it (`amend` / `accept-as-is` / `supersede`) all assume the *author* is
the failing party. None of them break a loop where the gate itself is
broken.

## Observation

Every wedged task carried the same deterministic-validation shape:

```bash
ENVIRONMENT=staging AWS_DEFAULT_ACCOUNT=000000000000 AWS_DEFAULT_REGION=us-west-2 \
  cdk synth <Stack> --quiet > /dev/null && make unit_test service=<svc>
```

The worker sandbox couldn't satisfy this. Pulling the actual stderr
from `events.payload->'output'->'validation_results'[].log_excerpt`
for the wedged task surfaced the smoking gun:

```
--- stderr ---
Traceback (most recent call last):
  File "/var/treadmill/workspaces/.../repo/app.py", line 3, in <module>
    import aws_cdk
ModuleNotFoundError: No module named 'aws_cdk'
python app.py: Subprocess exited with error 1
```

The `cdk` *binary* was present (PRs #28/#30/#31 added it on 2026-05-27
in response to a sibling failure earlier the same day). The Python
**`aws-cdk-lib` package** wasn't. RAMJAC's `app.py` does
`import aws_cdk` to build its constructs — and that fails before any
"AWS-side" question gets evaluated. The fix is one line in the agent
image's requirements (or a base image swap), not a sandbox networking
or credentials problem.

The author would write the code, the gate would exit 1, the author
would have no signal about *why* (the stderr was buried in the step
output, never surfaced through the architect's amend rationale), the
architect would say "amend" because the gate is red, wf-feedback would
analyze and re-author, the gate would exit 1 again. Five cycles. Cap.
Operator.

**Two failure modes braided.** The wedge had a tactical cause (missing
Python dep) and a structural cause (no mechanism to surface "gate is
tooling-broken" inside the loop). Fixing the tactical alone unblocks
the current backlog but the next missing-dep / wrong-tool will wedge
the same way. The structural fix (ADR-0058 `gate-broken` verdict)
prevents the next round.

## The root cause is upstream — in the plan, not in the loop

The plan skill produced a task whose acceptance gate the worker sandbox
*structurally cannot run*. No amount of code repair fixes that. The
loop has no escape valve. The cap is doing its job (bounding the
runaway) but the time and Anthropic-budget cost between dispatch and
cap is wasted — we knew within the first author cycle that the gate
was tooling-dead.

This is the same shape as the validation-gate-loop pattern documented
2026-05-26: a broken gate looks identical to broken code from inside
the loop. The 2026-05-26 case was a `cd ../cli` typo; this case is
`cdk synth` against an unreachable AWS. Different surface, identical
failure mode.

## The rule

**Every tool a deterministic gate invokes must exist in the worker
sandbox AND must not require network egress, live AWS, a real docker
daemon, or anything else the sandbox doesn't provide.**

Concretely banned in deterministic gates:

- `cdk synth` against a non-mocked account
- Any `aws ...` command that hits a live endpoint
- `docker run` / `docker compose`
- Network egress to packages or external services
- Tools not in the agent image

Concretely safe:

- `pytest <path>` (pytest is installed, the project's deps are
  installed at agent-image build time)
- `make unit_test` / `make lint` (provided the makefile's target
  doesn't itself shell out to forbidden tools)
- `grep` / `find` against the repo
- Anything that only reads `repo/` and writes nothing

If a check genuinely needs live AWS or docker, it belongs as:

- An `llm-judge` check that asks the model to reason about whether the
  code *would* synth / *would* run, OR
- A separate post-merge soak task, where the operator approves the
  environment before the soak fires.

## Generalization

Treat the worker sandbox as a **hermetic test environment** when
writing plans. The same discipline applied to unit-test design (no
internet, no live DB, no real clock) applies to deterministic
validation gates. If you'd be uncomfortable putting the check in a
CI smoke test, don't put it in the deterministic gate either.

## Follow-up artifacts

- `.claude/skills/plan/SKILL.md` updated (the gate-tooling clause was
  added to the existing "deterministic validation robust" bullet).
- ADR-0058 + plan: add a `gate-broken` verdict to the architect so a
  future deadlock surfaces to the operator on the FIRST detection
  instead of after the cap.
- Stderr-capture hotfix: the worker is truncating gate stderr before it
  reaches the architect; the architect has no way to diagnose what
  failed. Surface the full stderr in `StepOutput.validation_results`.
