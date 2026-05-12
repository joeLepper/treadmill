---
date: 2026-05-08
trigger: correction
status: captured
related: 2026-05-08-minimum-runnable-treadmill (plan)
---

# Learning: Test-client coverage is not the same as "runs locally"

## Trigger

Day 1A of the minimum-runnable-Treadmill plan declared completion based on three passing FastAPI `TestClient` unit tests. The user flagged the gap: *"Have we confirmed that Day 1A's output runs locally?"*

We had not. `TestClient` is an in-process ASGI client; it exercises the route table without ever booting a server, binding a port, or proving uvicorn comes up cleanly. Verifying the latter required actually starting `treadmill-api` and curling `/health` — which surfaced an unrelated but real defect: the default port 8080 was squatted on the dev machine.

## Observation

Three layers of "the API works" exist, and they are not the same:

1. **Route handlers handle the input shape they're given.** `TestClient` covers this. ASGI scope is fabricated; the server is never started.
2. **The server boots and binds.** Requires running uvicorn (or equivalent). Surfaces port conflicts, import-time errors not caught by tests, missing config, dependency resolution at startup.
3. **The server is reachable from where it needs to be reached.** Requires the network shape — Docker networks, host port mappings, the local adapter's wiring. This is the "live API in the spike substrate" the Phase 2 plan's Day 1 gate names.

The orchestrator declared layer 1 to be "Day 1A done" without acknowledging that the plan's Day 1 gate calls for layer 3.

## Generalization

When a plan declares a gate, the orchestrator should read the gate literally and check each clause against the work claimed. "Lands with: healthcheck endpoint + a passing test that hits the live API in the spike substrate" has two clauses; satisfying the first does not satisfy the second. Decomposing a plan day into sub-deliverables (1A, 1B, 1C, 1D) is fine, but the framing must be honest — "Day 1A done" overclaims when Day 1's gate is met by Day 1D's deliverable.

This is adjacent to the `2026-05-08-fabricated-supporting-evidence` learning (asserting more than evidence supports) but distinct: that one was about embellishment in artifacts; this one is about completion claims that outrun the gate.

## Proposed rule

Single observation; below threshold per `/rule`. Watch for a second instance.

If a second instance arrives, the rule shape:

> When a plan day or phase has a literal gate, the orchestrator must check the claim of completion against every clause of the gate text. Decomposition into sub-deliverables is permitted but does not transitively complete the day's gate; only the deliverable that satisfies the named clauses completes the gate.

## Proposed remediation

LLM-judge check on plan-execution status updates: parses the plan's gate text, compares to the artifacts the orchestrator claims as evidence, returns `pass`/`fail`/`uncertain`. Severity: warning, since plan gates often have prose that admits some judgment.

## Notes

The hook did not fire on the user's prompt — "Have we confirmed that Day 1A's output runs locally?" matches no trigger phrase. The phrase shape "have we confirmed..." or "are we sure..." is a question-form correction that the trigger list misses entirely. Worth adding as a future trigger, but the question-form-correction shape is broader than substring matching can cleanly capture.
