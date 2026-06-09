---
date: 2026-06-09
trigger: surprise
status: captured
related: plan-2026-06-08-adr-0084-coordinator-implementation
---

# Learning: Equivalence harness asserting 0 == 0 provides no coverage

## Trigger

PR #263 (synthetic trace fixture) shipped a dispatcher equivalence check that mocked
`dispatch_task` rather than the actual call surface (`publisher.publish`). The replay harness
ran, asserted 0 publisher calls vs 0 expected calls, and passed. The assertion was structurally
sound but covered nothing — old code and new code both produced zero calls because the mock
intercepted upstream of the real boundary.

Coverage was zero and invisibly so: there was no failing test, no skip marker, no warning.
The gap only surfaced in PR #267 when Bert swapped the stub for a real `Dispatcher` with a
recording publisher — which immediately produced 15 publish calls and exposed the actual
call surface for the first time.

## Observation

A passing equivalence assertion between two implementations of `f(x)` proves nothing if both
return the same empty result. The test's value is proportional to how much the expected
outcome constrains the space of valid implementations. `assert 0 == 0` constrains nothing.

## Generalization

When building an equivalence harness for a refactor, the gate is only meaningful if the
expected counts are derived from a real execution of the OLD code against a real (or
realistic) input, not from a stub that intercepts at a layer above the actual boundary.
A stub that prevents calls from reaching the measured boundary will always produce
identical (empty) results between old and new code regardless of correctness.

## Proposed rule

Equivalence harnesses must assert nontrivial expected values — counts > 0 for publish/DB-write
operations that should fire. Before merging a refactor, verify that the harness's expected
counts were obtained from a real execution of the pre-refactor code, not from a stub that
intercepts above the measurement point. A zero-vs-zero assertion is a code smell equivalent
to `assert True`.

## Proposed remediation

Add a lint-style check or test comment convention: when a recording mock records 0 calls,
emit a warning (`UserWarning`) rather than silently passing. Let the test author explicitly
assert `expect_zero=True` to suppress the warning. This makes the "zero is expected" case
intentional and visible.

## Notes

Fixed in PR #267 by using `treadmill_api.dispatch.Dispatcher` with a `RecordingPublisher`
injection. The recording publisher captured 15 `publish` calls — all previously invisible
through the stub. Related: [[plan-2026-06-08-adr-0084-coordinator-implementation]] Phase 5
trace-replay harness evolution.
