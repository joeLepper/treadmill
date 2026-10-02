---
date: 2026-10-01
trigger: pattern
status: captured
related: ADR-0105, ADR-0107
---

# Learning: The author is blind to their own fail-opens; the cross-family panel is not

## Trigger
Twice in one session the cross-model review panel BLOCKED an artifact I authored, each
time on a real defect I did not see:
1. PR #431 (the `--kind` feature OF the panel): the panel's own cross-family pass caught a
   FAIL-OPEN I introduced — `infer_kind` matched any path under `adrs/`/`plans/` without a
   `.md` check, so a code file (`src/plan/x.py`) would be judged a decision and its bugs made
   non-blocking. claude + gpt flagged it independently.
2. The client-MVP plan (`--kind plan`): the first pass caught a seal-vs-grading contradiction;
   after I fixed the decision, a second pass caught that a stale "grading runs in-AR" line
   still contradicted the fixed decision (gpt + claude + kimi, convergent).

## Observation
My own green read (unit tests passed; I had done an "adversarial" self-review) missed a
fail-open in gate tooling and two internal contradictions in a plan. A different-family
model found each on the first try.

## Generalization
We are blind to a specific class of defect in our own work: the relaxations we just wrote,
and the stale phrasing left behind after we edit one section but not its echoes. A same-mind
review (even a careful one) is primed not to see them. A cross-FAMILY panel is not.

## Proposed rule
Shared gate tooling and load-bearing plans get a cross-family panel pass before they merge or
settle; the author's own read never substitutes for it. For a change TO the gate tool itself,
the pass is mandatory — the author cannot certify their own fail-open.

## Proposed remediation
The panel gate already fails closed; keep it REQUIRED on review-panel PRs and run a
`--kind plan` pass before a load-bearing plan flips to `active`. When a pass blocks, re-run
after the fix — the block often moves to a NEW real defect (as it did here twice), so one
clean pass at the end is the bar, not one run.

## Notes
Validates ADR-0105 (cross-model pass) and ADR-0107 (the panel). Pairs with
[[2026-10-01-cross-model-review-judges-the-artifact-kind]] (why the panel needed `--kind`).
