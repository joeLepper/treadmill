# Plan: Architect `gate-broken` verdict

- **Status:** drafting
- **Date:** 2026-05-27
- **Related ADRs:** ADR-0058

## Goal

Give the architect a verdict (`gate-broken`) it can emit when the
deterministic gate is failing for reasons outside the author's control,
so ralph-loop deadlocks surface to the operator on detection #2 instead
of after the ADR-0029 cap fires. Unblock the RAMJAC backlog (5+
wedged tasks 2026-05-27) and prevent the same wedge for future plans.

## Success criteria

- A task whose deterministic gate exits non-zero with a sandbox-style
  failure (cdk synth without creds, docker without daemon, missing
  tooling) is verdicted `gate-broken` by the architect after at most
  2 author cycles, AND a `task.escalated_to_operator` event lands with
  `reason: gate-broken` and the gate's full stderr in the payload.
- The same task does **not** consume the amend-cap counter — operators
  see `architect_amend_count = 1` or `2`, not `5`.
- A legitimate amend cycle on a non-gate-broken task continues to
  consume the cap and verdict `amend` as today. (Regression guard.)
- The plan-skill SKILL.md's new sandbox-availability rule (landed
  2026-05-27) has a runtime enforcement mechanism — a plan that
  violates the rule surfaces fast.

## Constraints / scope

### In scope

- Schema change: extend `ArchitectVerdict.verdict` Literal and the
  worker-side `_VALID_VERDICTS` set.
- Architect prompt: Trigger B classifier added, with explicit cues for
  detecting sandbox-style failures (exit codes, "command not found",
  "Unable to locate credentials", etc.).
- Event: new `task.gate_broken` action (or extend
  `task.escalated_to_operator` with a typed `reason: gate-broken`
  field — pick one, prefer the latter for minimal new-event sprawl).
- Dispatch: gate-broken handling in `coordination/triggers.py` or
  `dispatch.py` — park the run, skip cap increment.
- Stderr-capture audit: confirm `validation_runtime.py`'s
  `log_excerpt[:2000]` reaches the architect intact; extend if the
  architect's context-injection layer re-truncates.
- Tests: unit tests for the new verdict path, integration test for
  the full architect → escalation event flow.
- Dashboard surface: render the gate-broken bucket distinctly
  (smallest possible — a tab or filter).
- ADR-0030 doc updates in `services/api/AGENT.md` and `workers/agent/AGENT.md`.

### Out of scope

- Letting the architect rewrite the gate. The operator owns gate
  repair; the architect surfaces, the operator decides.
- Auto-retrying gate-broken tasks after operator gate repair —
  manual `treadmill tasks retry` is the v1 path. A schedule-driven
  re-dispatch can come later.
- Sandbox-side fixes for `cdk synth` / `docker` failures themselves —
  separate work (the plan skill rule already steers plans away from
  those).
- Migrating historical wedged RAMJAC tasks. Operator can cancel +
  re-dispatch with corrected gates manually.

### Budget

Two focused sessions (one Treadmill orchestrator + one optional sweep
for the dashboard surface). ~2 PRs (one for the verdict + dispatch +
events; one for the dashboard tab). If we're at session 3 without a
working end-to-end, abort and post-mortem — the design probably
needs revisiting.

## Sequence of work

1. **Schema + parser** (~½ day) — extend `ArchitectVerdict.verdict`
   Literal + `_VALID_VERDICTS` set + the parser's prose-cue table in
   `runner_dispositions/architecture.py`. Unit tests for the new
   verdict parse path. **Files:** `services/api/treadmill_api/events/architect_verdict.py`,
   `workers/agent/treadmill_agent/runner_dispositions/architecture.py`,
   `workers/agent/tests/test_architecture_runner.py` (or sibling),
   `services/api/AGENT.md`, `workers/agent/AGENT.md`.
2. **Architect prompt — Trigger B classifier** (~½ day) — add the
   classifier section to the architect's system prompt; cues for
   exit-code style + "Unable to locate credentials" + ≥2 consecutive
   author cycles with the same gate output. **Files:** the architect
   role prompt YAML in `services/api/treadmill_api/starters.py` (or
   wherever the architect role's prompt currently lives). Include the
   existing test for the prompt + a new fixture covering Trigger B.
3. **Dispatch + event handling** (~1 day) — when the architect emits
   `gate-broken`: persist the verdict as today, but route to a new
   path that emits `task.escalated_to_operator` with `reason:
   gate-broken` and the full gate stderr in the payload, skips the
   amend-cap counter, parks the workflow_run in derived state
   `gate-broken`. **Files:** `services/api/treadmill_api/coordination/triggers.py`,
   `services/api/treadmill_api/dispatch.py`, the architect-cap counter
   query (likely in `triggers.py` or a SQL view).
4. **Stderr-capture audit** (~½ day) — trace the gate stderr from
   `validation_runtime.py:42` (`log_excerpt: str  # last ~2000 chars`)
   through to the architect's prompt. If any layer re-truncates,
   extend or stash to context-doc storage and reference. **Files:**
   `workers/agent/treadmill_agent/validation_runtime.py`,
   `workers/agent/treadmill_agent/runner_dispositions/architecture.py`
   (the context injector).
5. **Dashboard tab** (~½ day) — render gate-broken tasks distinctly in
   `services/api/treadmill_api/routers/dashboard/overview.py`'s
   `bucket` filter + a small dashboard UI change. **Files:** dashboard
   router + `services/dashboard/src/...` (smallest possible).
6. **Regression test pass** (~½ day) — services/api + workers/agent
   full suite green, including new tests. Smoke: dispatch a synthetic
   task with a `cdk synth` gate, verify gate-broken surfaces after 2
   cycles.

Tasks 1 + 2 are sequential (the parser must accept the verdict before
the prompt emits it). Task 3 depends on task 1. Tasks 4 + 5 + 6 can
parallelize once 3 is done.

## Risks / unknowns

- **Trigger B classifier false-positives.** The architect might
  classify a real author bug as gate-broken (the "Unable to locate
  credentials" cue could appear in legitimate test output). Mitigation:
  the classifier requires ≥2 consecutive cycles + structural cues
  from the stderr; the operator path is bounded.
- **Existing architect prompt is brittle.** Per the 2026-05-27 step.failed
  event ("architect summary contained no JSON block"), the architect
  occasionally emits prose-only responses. Adding a verdict increases
  the surface for malformed output. Mitigation: ADR-0053's
  prompt-tuning harness should A/B the new prompt against the old
  before cutover; a `step.failed` rate regression is an automatic
  abort.
- **Operator workflow change.** Operators get a new bucket to triage;
  if the bucket fills up but operators don't know how to repair gates,
  we've moved the wedge from the loop to the operator queue.
  Mitigation: ship the SKILL.md update FIRST (already done 2026-05-27)
  so new plans don't generate gate-broken candidates; the bucket
  should empty over time as old plans drain.
- **We'll abort if** the prompt-tuning harness shows a
  precision/recall regression >3 percentage points on the labeled
  architect corpus.

## Decisions captured during execution

(empty — populated as work progresses)

## Post-mortem

(filled in on completion / abandonment)
