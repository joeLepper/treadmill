---
date: 2026-06-10
trigger: pattern
status: captured
related: ramjac #1323 (logger sweep), #1265/#1310 (the three instances)
---

# Learning: bare MagicMock loggers accept any call shape — spec= moves the failure into the test

## Trigger
Three ramjac services crashed in production error paths on dict-positional
StructuredLogger calls (DED #1265, anonymizer, MAR #1310 layer 11). The
follow-up sweep (#1323) found the class extinct but answered WHY tests missed
all three: the suites inject bare MagicMock() loggers, which accept any call
shape; the TypeError only existed against the real logger, which only ran in
the error path, which only ran in production.

## Observation
A bare mock is maximally permissive: every interface drift between caller and
collaborator passes the unit test and surfaces at runtime — and error-path
calls surface at the worst runtime moment, when the handler is already
failing. mock.MagicMock(spec=StructuredLogger) made the same regression fail
in the unit test.

## Generalization
Mock permissiveness converts interface errors into never-executed-path
landmines. Anywhere we inject a mock for a collaborator with a real call
contract, spec= (or autospec) is nearly free and moves the failure to test
time. Our own stub-session tests (see
feedback_stub_session_tests_pin_call_order) are the same blindness class:
asserting calls happened without pinning the shape the real collaborator
accepts.

## Proposed rule
Mocks standing in for a typed collaborator carry spec=/autospec by default;
a bare MagicMock needs a reason.

## Proposed remediation
Reviewer-checklist line + (later) a lint for MagicMock() assigned to an
attribute whose real type is importable. Not wired anywhere yet.

## Notes
Ramjac is adopting it via #1323's review rider; this learning is the
treadmill-side export.
