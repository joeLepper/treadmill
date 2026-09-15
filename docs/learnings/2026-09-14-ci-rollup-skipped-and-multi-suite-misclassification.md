---
date: 2026-09-14
trigger: surprise
status: captured
related: ADR-0090, ADR-0115
---

# Learning: the coordinator §3.5 CI rollup mis-handles `skipped` and multiple github-actions suites

## Trigger
On the ADR-0113 netlify dogfood PR, one head had FOUR `github-actions` check-suites (the
repo has several `pull_request` workflows): 2 concluded `success`, 2 concluded `skipped`
(conditional workflows that no-op'd). Plus kodiakhq `neutral` and 11 external bot suites
parked forever in `queued`.

## Observation
Two gaps in the coordinator template §3.5 (main-merge CI gate):
1. **`skipped` is treated as failure.** §3.5 is binary — `conclusion == 'success'` →
   peer review, else → coordinator-rework. Its documented conclusion enum omits
   `skipped`, so a `skipped` github-actions suite hits the else branch and opens a
   spurious rework, treating a no-op conditional workflow as a CI failure.
2. **Multiple github-actions suites give conflicting decisions.** §3.5 assumes ONE
   github-actions suite ("act only on app_slug=='github-actions', one decision per
   rollup"). This repo produced four; success + skipped in the same head would fire both
   peer-review AND rework.
Separately (minor): 11 bot suites (netlify, renovate, coderabbitai, claude, cursor,
pkg-pr-new, spacelift, roadie, netlify-*-coding, netlify-staging-app) never leave
`queued`; the observer correctly emits nothing for them (it keys on `completed`), so they
are harmless — but a "wait for all checks" consumer would hang forever on them.

## Generalization
A binary success/not-success CI rollup is wrong on real repos: `skipped`, `neutral`,
`cancelled`, and multi-suite heads are common. "Not success" is not "failure". And a repo
can have many suites per app; there is no single github-actions rollup to key on.

## Proposed rule
A CI gate must treat non-failing conclusions (`success`, `skipped`, `neutral`) as
NON-BLOCKING and only `failure`/`cancelled`/`timed_out`/`action_required` as blocking;
and it must aggregate ALL relevant completed suites (no failure among them ⇒ pass),
never act on one suite as "the" rollup.

## Proposed remediation
For feature-branch mode this is MOOT — ADR-0115 removes the §3.5 CI gate entirely
(gate on peer review). For main-merge mode (still gated), fix §3.5: (a) add `skipped`
(and `neutral`) to the non-blocking set; (b) aggregate across suites (block only on a
real failing conclusion). Until fixed, main-merge on a repo with skipped/multi-suite CI
will spuriously rework.

## Notes
Caught live on the netlify dogfood 2026-09-14; the events were correct (5 ci_result with
the real mixed conclusions) — the defect is purely in the coordinator's interpretation.
