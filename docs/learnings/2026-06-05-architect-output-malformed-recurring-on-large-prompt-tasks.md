# Architect output malformed recurring on large-prompt tasks

**Date:** 2026-06-05
**Related:** ADR-0058 (gate-broken detector), ADR-0074 (nothing-to-do short-circuit), ADR-0081 (worker→operator hint channel)

## What happened

Four concurrent tasks (f839f914, da520f44, 92fb08dd, 86317e66) entered
the wf-feedback / wf-architecture-resolve loop on 2026-06-05 and burned
3-5 architect cycles each before being cancelled. Each task's architect
role produced output that the worker couldn't parse or act on — malformed
verdicts, ambiguous guidance, or structural issues the architect couldn't
see. The operator session had a clear diagnosis from cycle 1 in each case
(CI logs, PR diff, prior PR notes) but had no path to communicate that
diagnosis to the worker.

**Example patterns:**
- Task f839f914: The architect proposed a fix that conflicted with a
  prior bandaid from task e29ff9a1 on the same module. The operator saw
  the conflict immediately in the plan comments; the architect and worker
  had no visibility.
- Task da520f44: The test output was ambiguous about which assertion
  failed. The operator read the raw CI log and knew. The architect read
  the worker's summary and re-summarized it differently each cycle.
- Task 92fb08dd: The worker's prompt was too long for the architect's
  context; the architect's output was truncated mid-sentence. The
  operator could see the prompt size in the task metadata; the worker
  couldn't.

Each task hit the architect amend-cap after looping 3-5 times, then sat
in `capped` state until an operator force-retried with `--force-bypass-cap`
(still without a way to inject context, so the retry produced the same
wrong output again).

## Root cause

**The operator and worker are in fundamentally different information
asymmetries:**

- The **worker** has: the PR's code, the test output, the current state
  of the repo. Lacks: the full PR diff, prior changes to the module, the
  operator's notes on prior incidents, what worked last time.
- The **operator** has: the full PR diff, the CI logs, the prior PR
  comments, the plan's evolution, what *didn't* work last time. Lacks:
  a way to pass any of that to the worker mid-loop.

When the worker stalls, the operator can diagnose in seconds. When the
architect role tries to diagnose, it re-solves the same puzzle without
the operator's context, often getting a different (wrong) answer.

The architecture has no in-band channel for operator insight to reach
the worker. The only escape hatch is out-of-band: operator runs
`task retry --force-bypass-cap`, which re-runs the same prompt and
hits the same blocker again.

## Sibling pattern

This is the inverse of PR #208, where the operator committed a fix that
the worker then re-ran the same analysis on, saw a different answer, and
corrected the operator's fix. That was "worker reasoning beats operator's."
The 2026-06-05 cluster is the common case: "operator reasoning beats
worker's," and the system has no path to apply it.

## Impact on token economics

The four-task cluster cost:
- 4 tasks × 3–5 architect cycles × ~5–7K output tokens per cycle ≈
  **60K–140K tokens burned unnecessarily**, at Sonnet pricing ~2–5¢/1M.
- With operator insight injected at cycle 1, each task would have shipped
  in 1–2 cycles, with net cost reduction of 50–70% per task on the amend
  phase alone.
- The time cost to the operator was also high: manual retry, then manual
  re-fixing of the wrong output (some cases required hand-authoring a PR).

## Structural gap

Treadmill's dispatch model is operator-out-of-the-loop except for
escalations (ADR-0062). The assumption: "the worker + architect are
smart enough to converge." The 2026-06-05 cluster shows the blind spot:
when the worker is missing the operator's source context, both it and the
architect re-solve and diverge.

## What should change durably

**A two-layer hint channel (ADR-0081):**
1. Passive `operator_note` field on tasks that the worker reads at step
   entry and injects into the system prompt.
2. Active `request_hint` tool the worker can invoke mid-loop to relay
   context to the operator via cc-channels.

The operator sees the relay notification, reads the PR diff + CI logs, and
sets a hint. The worker's next step reads the hint and proceeds informed.
This is a small info-exchange cost (a few KB in the relay file, one ~500-char
note in the task state) against a recurring token-burn of 60K–140K per
four-task cluster.

**Measurement:**
- Audit trail via `task.operator_hint_requested` and `task.operator_hint_set`
  events lets us track: how often hints are requested, by which reason code,
  and whether they actually resolve the stuck loop (fewer architect cycles
  after hint injection).
- If hint quality is poor (false-fix rate high), the event log will surface it.
- If the hint mechanism becomes a band-aid instead of addressing the root class
  of failures, ADR-0075 §4's operator-obligation rule applies: recurring
  hint classes must trigger deterministic-detector implementation or
  structural fixes, not just hints.

## How an operator catches this pattern faster next time

When observing a task in a long architect-amend loop:
1. Check `workflow_runs` for this task: how many `wf-architecture-resolve`
   and `wf-feedback` steps? If >3 of the same role, something is looping.
2. Read the CI logs directly (not the worker's summary) — is the root cause
   obvious from the logs?
3. If yes, set an `operator_note` on the task with the diagnosis. The worker's
   next step will read it.
4. If the same *class* of fix gets requested on multiple tasks (e.g., "scope
   the existing test file" on three tasks), flag it as a signal to implement
   a deterministic gate instead of a repeated hint.
