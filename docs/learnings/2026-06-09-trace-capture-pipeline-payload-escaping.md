---
date: 2026-06-09
trigger: surprise
status: captured
related: ADR-0084, plan-2026-06-08-adr-0084-coordinator-implementation
---

# Learning: Trace capture pipeline double-serialized JSON payloads, producing malformed JSONL

## Trigger

Bert attempted to run the trace-replay baseline capture against Donna's 1453-event RAMJAC fixture (`coordination_trace_b0cd81fc_events.jsonl.gz`). 307 of 1453 lines (21.1%) failed to parse. The malformed lines share a pattern: unescaped `\"` sequences inside patch payloads — the capture pipeline serialized a diff-bearing payload field as a JSON string but missed a level of escaping, so embedded quotes terminated the outer JSON string early. The bug was pre-existing in Donna's original capture; the scrub script did not introduce it.

## Observation

The affected events are architect/feedback events with rich embedded content (shell commands, code snippets, markdown patches). These are the longest events in the trace and the ones most likely to contain quote characters. The 78.9% parseable events cover step lifecycle, task_prs writes, and audit rows — the core ADR-0011 single-writer paths — but architect/feedback routing coverage is lost.

## Generalization

Trace capture pipelines that serialize nested structured data (e.g. a JSON payload field that itself contains JSON-formatted strings with quotes) require an explicit extra `json.dumps()` call at each nesting level. A single `json.dumps(record)` call on the outer record will not escape quotes inside a payload field that was constructed as a raw Python string rather than a deserialized + re-serialized value.

## Proposed rule

Any trace/fixture capture script that serializes SQS/SNS event payloads to JSONL must: (a) round-trip the payload field through `json.loads` + `json.dumps` at capture time to normalize escaping, and (b) validate the output file with a parse-all-lines check before committing the fixture.

## Proposed remediation

Add a post-capture validation step to `scripts/capture_trace_baseline.py`:
```python
for i, line in enumerate(lines):
    try:
        json.loads(line)
    except json.JSONDecodeError as e:
        raise ValueError(f"Line {i+1} failed to parse: {e}") from e
```
This catches malformed output before it becomes a committed fixture. The capture script should fail loudly rather than produce a silently-partial fixture.

## Notes

- Immediate disposition: trace-replay harness in PR #258 ships with skip-malformed-lines + WARNING log + minimum-replay-count assertion (>=1000). The 78.9% coverage is accepted for v1.
- Follow-up: fix the capture pipeline, regenerate the fixture from a clean RAMJAC plan capture, update the committed baseline sidecar.
