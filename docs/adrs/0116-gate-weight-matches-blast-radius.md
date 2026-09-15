# ADR-0116: Gate weight matches blast radius — single cross-model evaluator on the integration branch

- **Status:** proposed
- **Date:** 2026-09-14
- **Amends:** ADR-0110 (feature-branch integration), ADR-0115 (feature-branch self-drive)
- **Related:** ADR-0108 (panel-backed evaluator), ADR-0105/0107 (cross-model review), ADR-0113 (PR-state polling), ADR-0114 (per-plan integration_base)

## Context

ADR-0115 removed the CI gate for integration-branch (feature-branch mode) task PRs
because the integration branch is not `main` — CI belongs on the human-owned branch→main
PR. The first collaborator-repo dogfood (`netlify/agent-runner-orchestrator`, ADR-0113)
then exposed the SAME over-gating class in the EVALUATOR: a docs-only finding note went
through the full ADR-0108 cross-model PANEL evaluator + up to 3 rework cycles — ~11
minutes PER cycle, ~1 hour total, on one docs finding (the panel fans out to multiple
model families, and the gateway was degraded).

But the evaluator is NOT pure overhead the way a green CI matrix on a docs change was:
on this very finding, peer review PASSED (2× lgtm) and the evaluator STILL caught a real
correctness defect — the note claimed the Codex CLI emits no turn-wrapper event, which 3
model families contradicted (`codex exec --json` emits `turn.started/completed`). The
value is an UNBIASED agent comparing the incoming task + its validation step to the diff,
without the bias of having planned or implemented the work. Dropping it (peer-review-only)
would have LANDED the wrong finding. And the rework loop "has done us well in Tapestry AND
Treadmill" (operator, 2026-09-14) — losing it is not acceptable.

## Decision

We decided the gate weight MATCHES the blast radius, and the expensive part of the
evaluator is the PANEL BREADTH, not the evaluation itself:

- **Integration-branch task PRs (feature-branch mode): LIGHT.** Peer review → the
  evaluator runs with a **SINGLE cross-model** pass (`panel.py … --min-cross-model 1` —
  ONE cross-family model, unbiased vs the same-family implementer) **plus the full
  §9.4/§9.5 rework loop, kept.** We KEEP the evaluator and the loop (the correctness catch
  and the iteration that has served us well); we lighten ONLY the panel fan-out (the ~11
  min/cycle cost source) from a multi-model panel to one cross-family voice.
- **Integration→`main` promotion PR: HEAVY, once.** The FULL cross-model panel
  (`--min-cross-model 2`) + full CI run ONCE at the human-owned branch→main promotion,
  where the change actually reaches `main`. (For a non-`main`-base deliverable — ADR-0114,
  where the integration branch itself is the deliverable and there is no branch→main PR —
  the human's review of the branch is that heavy gate.)
- **Main-merge mode (`merge_target=main`): UNCHANGED.** The rare agent-merge-to-`main`
  repo keeps the full panel per task.

## Alternatives considered

- **Incumbent: full panel evaluator per integration-branch task (ADR-0108 as-is).** Why
  insufficient: ~1hr/3-cycle on a docs finding whose blast radius is an integration
  branch, not `main`. The panel's breadth is disproportionate to the branch's stakes.
- **Peer-review-only (drop the evaluator on the integration branch).** Rejected on
  EVIDENCE: peer review passed the slice-1 finding 2×, and the evaluator still caught the
  Codex-turn-events correctness defect — peer-review-only would have landed a wrong
  finding. "Lighter touch on evaluations" meant light, not zero.
- **One-shot evaluator with NO rework loop.** Rejected: the rework loop is a proven
  quality mechanism (Tapestry + Treadmill); a single verdict with no iteration loses the
  fix cycle that turns a caught defect into a corrected deliverable.
- **Keep the full panel but cache/parallelize it.** Rejected as the fix here: the panel's
  cost on a degraded gateway is the fan-out itself; a single cross-family voice is
  proportionate for the integration branch, and the full panel still runs at promotion.

## Consequences

### Good
- Integration-branch findings clear in minutes, not ~1hr, while KEEPING the unbiased
  cross-model correctness catch AND the rework loop.
- The heavy full-panel + full-CI gate runs exactly once, at the point of real blast
  radius (branch→main promotion / the human's deliverable review).

### Bad / trade-offs
- A single cross-family voice is narrower than the full panel — it may miss a defect a
  second non-Anthropic family would catch. Mitigated: the full panel runs at promotion,
  and peer review + the same-family evaluator context still apply per task.
- Two evaluator weights to maintain (single on integration branch, full at promotion /
  main-merge), keyed on `merge_target` + gate position.

### Risks
- **Falsifier:** in `feature-branch` mode, an integration-branch task PR runs the FULL
  `--min-cross-model 2` panel (not the single `--min-cross-model 1`), OR the promotion /
  main-merge path runs only a single-model pass instead of the full panel — either means
  the gate weight no longer matches the blast radius.

## References

- `tools/team-templates/coordinator/CLAUDE.md.tmpl` §9.1 (brief the evaluator for a single
  cross-model pass in feature-branch mode).
- `tools/team-templates/evaluator/CLAUDE.md.tmpl` (`--min-cross-model 1` for an
  integration-branch task; `--min-cross-model 2` for main-merge / promotion).
- ADR-0115 (removed the CI gate for the same blast-radius reason); ADR-0113 dogfood
  evidence (slice-1: 3 evaluator cycles / ~1hr, evaluator caught a real defect).
