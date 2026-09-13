# ADR-0108: Worker-code review is panel-backed

- **Status:** proposed
- **Date:** 2026-09-13
- **Related:** ADR-0087 (team execution / evaluator role), ADR-0105 (cross-model review), ADR-0107 (fleet model gateway + review panel)

## Context

ADR-0087 §9 makes the `evaluator-<repo>` session the final quality gate before a
worker PR merges: it reads the diff, CI, and peer-review thread, then returns a fixed
`[verdict: approve | rework]` the coordinator routes on. The evaluator is a Claude Code
session — the SAME model family as the workers it audits. ADR-0105 established that a
review from the author's own family shares its blind spots, and that cross-model
(non-Anthropic) voices catch failure modes the same-family reviewers miss; we already
route ADR/plan AUTHORING reviews through the ADR-0107 cross-model panel and retired the
Tapestry evaluator agents. The worker-code review gate was still a solo same-family
read. The operator directed (2026-09-13) that it use the panel too.

## Decision

The evaluator's review is **panel-backed**: before forming a verdict, the evaluator
runs the ADR-0107 panel on the PR diff (`--author-family claude --min-cross-model 2`)
and folds the result into its single fixed verdict. The ADR-0087 lifecycle is
unchanged — the coordinator still parses `approve | rework`. Specifically:

- A panel **BLOCK** defaults to `rework` (findings → remediation). The evaluator MAY
  override a block it judges spurious or diff-injected, but the override is SURFACED to
  the coordinator; for a load-bearing PR (schema/migration/contract/infra/security) an
  override of a cross-model BLOCK ESCALATES to the orchestrator, never merges on the
  evaluator's justification alone.
- A panel **approve** is NECESSARY-NOT-SUFFICIENT: the evaluator still applies its own
  holistic checks (scope/artifact discipline, repo rules, task fit).
- **Reduced coverage** (fewer than two non-author families voted — e.g. a Go cap) is a
  fail-closed signal from the panel: surfaced to the coordinator; a load-bearing PR
  HOLDS/escalates rather than merging with a note.
- **Large diffs** are size-checked BEFORE the panel (input truncation is invisible in
  output finish_reason) and chunked or escalated.

The evaluator stays read-only in the Treadmill-API sense; running the panel is model
calls + a temp file outside the worktree, not an API write.

## Alternatives considered

- **Incumbent: the solo same-family evaluator read (ADR-0087 §9).** Why insufficient:
  the evaluator is Claude, like the workers; it shares their blind spots and adds no
  cross-model voice — exactly the gap ADR-0105 names, left open on the code gate.
- **Fully replace the evaluator with the panel.** Rejected: loses the repo-context
  holistic judgment, the scope/artifact/rules gate, and the fixed-verdict contract the
  coordinator depends on; the panel reviews a diff with little surrounding context and
  is weakest on cross-file/semantic issues. Panel = necessary-not-sufficient INPUT to
  the evaluator is the right shape.
- **Tapestry evaluator agents for cross-model review.** Rejected by ADR-0105 (route to
  the panel/siblings, not Tapestry).

## Consequences

### Good
- Genuinely non-Anthropic voices now gate worker code, not just authoring artifacts.
- One mechanism (the panel) covers both authoring and code review; the coverage
  enforcement (`--author-family`/`--min-cross-model`) retrofits both at once.
- The evaluator remains the authority and the single-verdict interface is unchanged.

### Bad / trade-offs
- Panel latency is added to each evaluator cycle (acceptable; bursty-but-rare role).
- Diff-only cross-model review is weakest on cross-file/semantic defects — those stay
  the same-family evaluator's job.
- Coverage now depends on OpenCode Go availability; a cap reduces coverage (surfaced +
  held for load-bearing, never silently).

### Risks
- **Falsifier:** a load-bearing PR merges while a panel BLOCK was overridden without an
  orchestrator escalation, OR while cross-model coverage was reduced with no surfaced
  `reduced-coverage` marker — i.e. code merges past a defeated/absent cross-model
  layer with no visible trace.

## References

- docs/plans/2026-09-13-evaluator-code-review-via-panel.md (implementation + review).
- tools/team-templates/evaluator/CLAUDE.md.tmpl (the panel-backed review section).
- tools/model-review-panel/ (the panel + coverage enforcement).
