---
date: 2026-05-11
trigger: correction
status: captured
related: ADR-0011, ADR-0010, plan:2026-05-08-minimum-runnable-treadmill
last_crystallization_check: 2026-05-17
crystallization_backoff_until: 2026-05-24
crystallization_target: pending-second-instance
---

# Learning: Don't close phases on unresolved review findings — pedanticism compounds at the foundation

## Trigger

After closing Week 2 of the minimum-runnable-Treadmill plan, the orchestrator was preparing to begin Week 3 with a few high-priority debts banked. The user manually spun up an adversarial reviewer (because the auto-review wiring is not built yet) which surfaced ~30 findings across eight buckets — including real load-bearing issues like an ADR-0010 branch-format violation, a missing `task_prs` writer that breaks the webhook→trigger chain, the worker→consumer Pydantic-boundary contract honored only on one side, and the dry-run smoke being passed off as end-to-end verification of Phase 2 success criterion 4.

The orchestrator offered the user two paths: (a) re-run the smoke against real Claude Code and pivot to a Week 2.5 cleanup pass, or (b) bank the debts and push into Week 3.

The user took option (a) emphatically: *"I don't think that we can close week 2 until all of this is resolved. We're at the very beginning of a large architectural shift. If there's a time to be pedantic it's right now."*

## Observation

The orchestrator's instinct after a productive stretch is to declare a milestone and move forward. The plan running log dated 2026-05-08 said "Week 2 complete." But a stretch that ships 50 tests, an event-driven coordination consumer, a dispatcher, a real worker harness, real bare-repo provisioning, and a live smoke can *still* be wrong-shaped if it ships those things with architectural drift baked in.

Two specific drift mechanisms appeared:

- **Stub-as-success.** The smoke ran with `TREADMILL_AGENT_DRY_RUN=1`, so the worker wrote a `.treadmill/<step_id>.md` marker rather than authoring real code. Phase 2 success criterion 4 says "an authoring worker picks up a task, branches, authors the change." A marker file is not an author. The closure note read "End-to-end smoke verified live" — technically true, architecturally false.
- **Half-honored contracts.** ADR-0011's "Pydantic at every boundary" was honored only on the API publish path; the worker built raw dicts. ADR-0010's branch format `task/<short-id>-<slug>` was replaced with `task/<short>/<step_name>` and a test was written to assert the wrong format. Each is a one-line drift; the cost of letting these compound across Weeks 3 and 4 is asymmetric — every downstream tool that takes a dependency on the wrong shape has to be retrofitted later.

## Generalization

At the beginning of a large architectural shift, the foundation is plastic and the cost of dragging drift forward is *high*. The orchestrator's natural rhythm — *ship, declare done, move on* — must be tempered with a second beat: *review against the contracts before moving on*. The contracts are the ADRs and the plan-doc. The mechanism is an adversarial reviewer reading the just-shipped code against those contracts.

Project rhythm by phase position:

- **Early architectural shift (now)**: every phase boundary triggers a full review-against-contracts pass. Findings are *blocking* on phase closure, not banked.
- **Steady-state operation (later)**: findings can be bucketed; severe ones block; minor ones bank.

We are in the early phase. Pedantry now is the cheapest it will ever be.

## Specific drift that triggered this learning

The 30 findings surfaced by the reviewer fall into clusters. The orchestrator (correctly) suggested either a Week 2.5 cleanup or banking the debts. The user (correctly) rejected the bank-and-defer framing entirely: *closing Week 2 with these findings open* is not a permissible state, regardless of whether Week 2.5 is the label or "Week 2 finishing" is. The plan's "Week 2" entry stays open until the findings are addressed.

## Proposed rule

A candidate. Watch for a second instance — a future temptation to close a phase with known unresolved findings — before crystallizing. If a second instance arrives, the rule shape is something like:

> *Phase closure requires an adversarial review against the ADRs and active plan-doc. Findings are blocking until either (a) addressed in code or (b) explicitly converted into a tracked follow-up plan or ADR. "Bank and defer" is not a valid finding disposition at the start of an architectural shift.*

The dispatch mechanism for the reviewer is currently manual (the user spins one up). When the auto-review wiring lands (the planned `/ultrareview` analogue inside Treadmill itself), this rule's enforcement moves from convention to substrate.

## Proposed remediation

None yet — wait for the rule. But the mechanism is already exercisable: an `adversarial-reviewer` agent invocation pointed at the just-shipped code surface plus the relevant ADRs plus the active plan-doc, returning a structured findings report. The first successful instance is in this conversation's transcript.

## Notes

The orchestrator framed the choice as "Week 2.5 cleanup OR bank the debts" — implicit framing that *Week 2 is already closed*. The user's correction was sharper than rejecting the second option: it rejected the framing. The reframe is: *Week 2 is not closed until the findings are addressed.* The reviewer's findings are the new Week 2 backlog.

This pairs with `2026-05-08-fabricated-supporting-evidence.md` — both are about resisting the orchestrator's instinct to declare success when the evidence is partial.
