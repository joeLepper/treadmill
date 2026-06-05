# Plan: ADR-0058 residual cleanup

- **Status:** drafted
- **Date:** 2026-06-05
- **Related ADRs:** ADR-0058
- **Supersedes:** none

## Goal

Close out the residual surfaces of ADR-0058 (`gate-broken` architect verdict) now that Steps 1-6 are merged. The substantive design is shipped:

- `ArchitectVerdict.verdict` Literal carries `gate-broken` + `gate_log_excerpt` (`services/api/treadmill_api/events/architect_verdict.py`)
- Worker-side disposition extends `_VALID_VERDICTS` + `_PROSE_VERDICT_CUES` (`workers/agent/treadmill_agent/runner_dispositions/architecture.py`)
- Architect prompt has the Trigger B sub-classifier (`services/api/treadmill_api/starters.py`)
- API-side trigger emits `task.escalated_to_operator` with `reason='gate-broken'` (`coordination/triggers.py::maybe_dispatch_gate_broken_escalation`)
- Consumer wires it after `step.completed` (`coordination/consumer.py::_maybe_dispatch_gate_broken_escalation`)
- `TaskEscalatedToOperator` payload carries `reason` + `gate_log_excerpt`
- Dashboard overview honors `?reason=gate-broken` filter
- Tests: `test_architect_verdict.py`, `test_gate_broken_trigger.py`, `test_gate_broken_end_to_end.py`, `workers/agent/tests/test_log_excerpt_pipeline.py`

What remains is small: stale routing-payload cleanup in the worker disposition, an explicit assertion that the workflow_run "parks" naturally (rather than via a new state column), and a one-line AGENT.md sweep where the May 28 plan left `[#TBD]` placeholders.

## Success criteria

1. Worker-side `architecture.py::_build_dispatch_payload`'s `gate-broken` branch returns a non-inert payload (or no payload at all). The comment block referring to "Step 1 — schema-only landing... pending Step 3" is removed or rewritten now that Step 3 is shipped. The behavior change is null — the consumer routes gate-broken via the trigger keyed on `step.completed`, not via the disposition's dispatch payload — so this is documentation + intent-tag cleanup, not a code-path change.
2. A short test (or a one-line assertion in an existing test) pins that after a `gate-broken` verdict fires and the escalation lands, no further `WorkflowRun` is dispatched against the task by the consumer's routing. This is the "park as deferred" outcome the ADR Decision §3 names: a parked run is one with no successor. The current implementation achieves it implicitly (the gate-broken branch returns no new workflow_id); the test pins that against future drift.
3. `workers/agent/AGENT.md` line ~39 and `services/api/AGENT.md` line ~71's `[#TBD]` placeholders are filled with the actual merged PR numbers (`#50` / `#53` / etc. — sourced from `git log --grep ADR-0058`).
4. No behavior change observable to existing tests; the suite stays green.

## Constraints / scope

### In scope

- `workers/agent/treadmill_agent/runner_dispositions/architecture.py` — the `gate-broken` branch of `_build_dispatch_payload` (~lines 424-438) and its comment.
- One new test (or an extension of an existing one) in `services/api/tests/` or `workers/agent/tests/` pinning the "no successor run" invariant.
- AGENT.md sweep: replace `[#TBD]` with actual PR numbers for ADR-0058 entries.

### Out of scope

- **Frontend dashboard rendering** of the gate-broken bucket distinctly. Backend has the filter; the UI badge work belongs on the dashboard track.
- **A new `workflow_runs.state='gate-broken'` column.** The ADR mentions "derived state" but the existing escalation-event projection plus the lack of a successor run already give operators + sweeps the signal they need. Adding a column without a consumer that reads it is dead code.
- **Re-touching the architect prompt.** The Trigger B classifier shipped + has held since 2026-05-28.
- **Cap-counter changes.** Already correct — `gate-broken` isn't `amend`, so the amend-cap counter naturally doesn't advance.

### Budget

One PR, hand-authored (small enough to not warrant worker dispatch). Estimated <30 min of edits + tests.

## Sequence of work

1. Read `workers/agent/treadmill_agent/runner_dispositions/architecture.py` lines 424-438. Either:
   - **(a)** Remove the `gate-broken` branch entirely from `_build_dispatch_payload` and let the function raise `ArchitectVerdictParseError` on `gate-broken` — IF the consumer never reads the dispatch payload for gate-broken (which appears to be the case; the trigger keys on `step.completed.payload.verdict`, not on a dispatch).
   - **(b)** Keep the branch but rewrite the comment to reflect the shipped state and rename `intent` from `"gate-broken-await-step-3"` to `"gate-broken-escalation"` to remove the temporal-stale wording.
   
   Decide between (a) and (b) by reading `_build_dispatch_payload`'s callers — if any of them inspect the returned dict for `gate-broken`, prefer (b). Otherwise (a) is cleaner.

2. Add a unit test (or extend `test_gate_broken_end_to_end.py`) asserting that after the gate-broken escalation event fires, no follow-up `WorkflowRun` insertion is staged by the trigger or consumer. The shape: stub the session, drive `_maybe_dispatch_gate_broken_escalation`, assert `dispatcher.persist_and_publish` was called exactly once and the call's `entity_type/action` is `task/escalated_to_operator` — no `workflow_run.registered` or sibling dispatch event. This is the operationalization of the ADR's "park as deferred."

3. Run `git log --grep "ADR-0058"` to find the merged PR numbers for the Step 1, 3, 4, 6 entries currently marked `[#TBD]` in `workers/agent/AGENT.md` and `services/api/AGENT.md`. Replace the placeholders with the real PR numbers.

4. Update `workers/agent/AGENT.md` Recent-changes with a one-line entry naming the disposition cleanup + the new invariant test, citing this plan.

## Risks / unknowns

- **(1a) vs (1b) wrong choice.** If `_build_dispatch_payload`'s `gate-broken` return value IS consumed downstream (e.g., a worker-side disposition-publisher that emits the dict as an event payload), option (a) would silently drop the gate-broken verdict from the worker's emitted disposition event. Reading the call sites is the gate. If unclear, prefer (b).
- **AGENT.md PR-number archeology.** If multiple ADR-0058 PRs merged with similar commit subjects, distinguishing which `[#TBD]` is which may take a `git log -p --grep ADR-0058 -- <path>` per file. Not load-bearing — wrong PR number is a docs nit, not a behavior bug.

## Diagram

Skipped — purely textual cleanup; the ADR-0058 ADR carries the end-to-end sequence diagram.

## Decisions captured during execution

(empty)

## Post-mortem

(filled in on completion)
