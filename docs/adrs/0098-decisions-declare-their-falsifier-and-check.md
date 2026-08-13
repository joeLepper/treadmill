# ADR-0098: Decisions declare their falsifier and their check

- **Status:** accepted (2026-08-13, operator)
- **Date:** 2026-08-12
- **Related:** ADR-0006 (rules and remediations primitive), ADR-0003 (three-layer documentation model), ADR-0034 (learnings-to-rules crystallization)

## Context

The `/decide` skill already requires falsifiability of the decision itself: "The decision must be falsifiable: a future reader should be able to tell whether the system honors it." That sentence asks the author to write a decision that *could* be checked. It does not ask for the check, and it does not ask what a violation would look like.

`exec_otp` shows the cost of that gap. It accumulated 27 architecture decision records over 36 days. None of them prevented the failures that ended it. The sharpest case is its ADR-0013, which designed a deferred move for a busy agent at its next idle boundary — precisely the defect that later made agents unmovable. It was written, agreed, and never built, and nothing in the system noticed. Its postmortem §4.9 records the consequence: relocating a busy agent required hand-authoring a loop that called the migration function every 250 ms until it succeeded.

A decision record that is never contradicted by evidence is indistinguishable from one that is never honoured. Treadmill already has the missing half. ADR-0006 defines rules with `checks`, each `deterministic` (a script that "exits 0 = pass, non-0 = fail") or `llm-judge`. We have the mechanism and do not connect it to decisions.

## Decision

We decided that **every architecture decision record states its falsifier, and either binds a check or declares itself unchecked and says why.**

1. **Falsifier.** Each ADR names the concrete observation that would mean the system no longer honours it. Not a restatement of the decision — the symptom a reader could witness. "If a message is delivered twice to a handler, this decision is not being honoured."
2. **Check, or an explicit gap.** Each ADR either references a check under `docs/knowledge-base/rules/` that detects its falsifier, or states `Check: none` with a reason. Both are acceptable outcomes; silence is not.
3. **The reviewer checklist gains one question**, alongside the existing incumbent question: does this ADR name a falsifier, and is the check binding present or the gap declared?

We require the declaration, not the check, because many decisions are policy and cannot be mechanised honestly. A ceremonial check that always passes is worse than a recorded gap: it converts an unknown into a false assurance.

## Alternatives considered

- **Incumbent: the `/decide` skill's falsifiability sentence.** **Why insufficient:** it constrains how the decision is *phrased* but produces no artefact. Nothing records what a violation looks like, and nothing detects one. `exec_otp` ADR-0013 satisfied the incumbent rule completely and still went unbuilt and undetected.
- **Require a passing check for every ADR.** Rejected: policy and scope decisions have no honest mechanical test. Forcing one yields checks written to pass, which is the failure mode we are trying to avoid.
- **Track implementation status on the ADR (`proposed` → `implemented`).** Rejected as insufficient alone: status records agreement and delivery at a point in time, while conformance regresses afterwards. `exec_otp`'s hot-loaded fix was implemented and then silently lost to a restart. A check catches the regression; a status field does not. We would accept this in addition, never instead.
- **Rely on review to catch unhonoured decisions.** Rejected on the project's own evidence: the reviewers of ADR-0084 approved cc-relay as the primary transport on the grounds that it was "proven in production," and we later retired it for causing "real message loss — #165." Review inside the proposer's frame did not surface the defect; operating the system did. The skill already cites `2026-06-11-check-the-incumbent-before-designing.md` for why.

## Consequences

### Good
- The set of decisions with no check becomes visible and countable, instead of being assumed covered.
- A regression in a previously honoured decision can be detected rather than rediscovered during an incident.
- Falsifiers give plans a concrete acceptance target: build the thing that makes the check pass.

### Bad / trade-offs
- Every ADR costs more to write.
- Checks are code, so they need maintenance, and a stale check is its own liability.
- Authors may write weak falsifiers to satisfy the template. The reviewer checklist is the only guard.

### Risks
- The rules directory becomes a graveyard of unrun checks if nothing executes them on a schedule. A check that never runs is a declared gap wearing a costume.
- **Falsifier:** if an ADR merges with no falsifier line and no `Check:` line, this decision is not being honoured.
- **Check:** deterministic script over `docs/adrs/*.md` asserting both lines are present. To be written.

## Diagram

Omitted. This decision is policy, and the `/decide` skill directs us to skip a diagram when the decision describes no system interaction.

## Follow-ups

- Amend `.claude/skills/decide/SKILL.md`: add the two required lines to the template and the question to the reviewer checklist.
- Write the deterministic check named above.
- Decide whether existing ADRs are backfilled, or whether the requirement applies only from ADR-0093 onward.

## References

- `/decide` skill, "Decision" section and reviewer checklist.
- ADR-0006 (rule `checks` schema).
- `exec_otp` postmortem §4.9 and §6 rule 5: `~/obsidian/lepper/exec-otp/exec_otp Postmortem.md`.
