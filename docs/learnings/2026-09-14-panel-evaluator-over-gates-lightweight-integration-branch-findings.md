---
date: 2026-09-14
trigger: pattern
status: crystallized-into-ADR-0116
related: ADR-0108, ADR-0115, ADR-0116
---

# Learning: the panel-backed evaluator over-gates lightweight integration-branch findings — but catches real defects

## Trigger
On the ADR-0113 netlify dogfood, a DOCS-ONLY finding note (a task on the
`joes-agents/<slug>` integration branch, `merge_target=feature-branch`) went through the
ADR-0108 panel-backed evaluator: ~11 minutes PER rework cycle (cross-model panel), up to
3 cycles (§9.5 cap). The evaluator ran AFTER peer review already passed (2× lgtm).

## Observation
The panel-backed evaluator is the SAME over-gating CLASS that ADR-0115 removed for CI:
a heavyweight per-task gate applied to every finding landing on an integration branch,
where the human owns the real gate at the branch→main PR. ~11 min × up-to-3 cycles is a
large latency tax on a docs note. BUT — a key difference from the CI matrix: the
evaluator CAUGHT REAL DEFECTS (4 content defects on this finding, after all 13 citations
verified exact). It is not pure overhead the way a green CI matrix on a docs change was.

## Generalization
"Gate weight should match the branch's stakes" applies to the EVALUATOR as it did to CI:
an integration branch (human-reviewed deliverable, not `main`) may not need the full
per-task adversarial panel that a merge-to-main would. But unlike CI, the evaluator
produces genuine quality signal, so lightening it is a quality/speed TRADEOFF, not a
clear win — the decision is the operator's, not an obvious cut.

## Proposed rule
RESOLVED by ADR-0116 (2026-09-14): gate weight matches blast radius, and the expensive
part is the PANEL BREADTH, not the evaluation. Integration-branch task PRs run a SINGLE
cross-model pass (`panel.py --min-cross-model 1`) and KEEP the §9.4/§9.5 rework loop; the
FULL panel (`--min-cross-model 2`) + full CI run once at the integration→`main` promotion.
The tradeoff was decided by lightening the cost (panel fan-out), not the value (the
unbiased cross-model catch + the loop) — which is exactly what this learning surfaced.

## Proposed remediation
Landed in ADR-0116. Coordinator §9.1 sets `panel_breadth: single|full` by `merge_target`;
the evaluator template reads it (`--min-cross-model 1` on the integration branch, `2` at
promotion / main-merge). The ADR-0115 CI-gate removal + inline-depends_on stay as-is.

## Notes
Distinct from the CI-gate finding (ADR-0115) precisely because the evaluator earns its
latency (real defects) while the CI matrix did not. Captured at Carla's request during
the netlify dogfood.
