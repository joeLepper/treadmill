# ADR-0048: Architect widens from arbitrator to recoverer; verdict surface collapses to three actionable values

- **Status:** accepted
- **Date:** 2026-05-19
- **Related:** ADR-0029 (ralph-loop validation runner), ADR-0031 (auto-merge on mergeable), ADR-0032 (role-architect verdicts), ADR-0037 (author-side feedback), ADR-0038 (ralph-loop deadlock arbitration), ADR-0042 (validate.override channel), ADR-0044 (datetime-keyed migration revision IDs), ADR-0046 (operator retry CLI), ADR-0047 (deterministic state in auto-merge path)
- **Reads with:** [task-flow diagrams in `docs/diagrams/task-flow-*.md`](../diagrams/task-flow-overview.md) (PR #178). The diagrams' [dead-end catalog](../diagrams/task-flow-dead-ends.md) is the shared vocabulary this ADR's decisions attach to.

## Context

The 2026-05-19 stuck-task audit found 24 fail-state tasks across open plans. Bucket-A retry experiments showed that **the largest class of stuck tasks aren't transient failures — they're logical fixed-points**: the worker concluded there's nothing to do, the system's recovery loop (wf-feedback) also concluded there's nothing to do, the chain terminated silently with no PR ever opened. The retry CLI from ADR-0046 was designed for transient errors and is structurally wrong for impasses.

Two architectural gaps surfaced from the audit:

1. **The recovery loop assumes a PR exists.** wf-feedback's job is to look at a gate-bearing failure (validate fail / review changes-requested) and remediate. When wf-author produced no diff, there's no PR — yet ADR-0037 still dispatched wf-feedback. Feedback then soft-completed with `responded-without-change`, and the deadlock-arbitration predicate (which requires a blocking gate signal) didn't fire. The chain dead-ended without notification.

2. **The architect can't rewrite the plan.** When the LLM looks at a task and decides "nothing to do" reproducibly, the only way to escape is to change the input. Today the task description is immutable from registration to terminal. The architect can emit `amend` (a remediation plan that wf-feedback hands the next author iteration) but cannot rewrite the task text itself. For tasks where the original spec is the problem, no remediation hint will produce different output.

These gaps converge on the architect role. **Today the architect is an *arbitrator* — it intervenes when ralph-loop participants disagree.** We are widening the role to *recoverer* — the architect decides how to recover from any non-mergeable terminal where the system has run out of cheaper escalations.

ADR-0048 was a narrower proposal that framed the problem as wf-feedback's responsibility (escalate to architect on no-diff completion). Joe's review on 2026-05-19 reframed it: the wf-feedback dispatch on author-no-diff is itself wrong-shaped — there's nothing for feedback to remediate when there's no PR. The right route is for author-no-diff to go directly to architect-on-plan, skipping wf-feedback entirely. This ADR captures that reframe and supersedes ADR-0048.

## Decision

### 0. The architect's job is to increase clarity, not to lower the bar

Widening the architect's responsibilities (gate-override + plan-rewrite + escalation) widens the surface for a well-documented LLM failure mode: **when an LLM has the power to accept its own work as good enough, it tends to rationalize accepting work that doesn't meet the bar, rather than insisting the bar be met.** The instinct is to "declare victory" — to make the problem go away — rather than to sit with friction.

The architect role concentrates this risk: `accept-as-is` overrides a blocking gate, and `supersede` rewrites the task itself. Both verdicts give the LLM a path to make a stuck task stop being stuck without producing the work the original spec called for. Without explicit guardrails, the architect will reach for these levers in cases where the correct move is "the bar is right; the work is wrong; iterate."

**The architect's job is to make the work clearer, not to make the bar lower.**

Concretely, this is what the architect must internalize and what the role-architect prompt must encode:

- **`accept-as-is` is for cases where the gate is wrong, not where the work is "close enough."** If a reviewer or validator flagged something real, and the diff doesn't address it, the architect's correct verdict is `amend` (iterate with a hint) or `supersede` (rewrite the spec to make the missing pieces explicit) — not `accept-as-is`. The override channel is for miscalibrated gates, not for shipping under-spec work.
- **`supersede` is for clarifying the task, not making it easier.** When rewriting the description, the architect's `rewritten_description` must preserve the original task's intent and acceptance criteria. The rewrite makes the work more actionable (clearer file paths, specific behaviors named, ambiguity resolved) but does not narrow scope to whatever the worker happened to produce. "Make it easier so this passes" is the anti-pattern; "make it clearer so a competent author can succeed" is the pattern.
- **`amend` is the default escalation, not the fallback.** When in doubt, iterate. The cap on `wf-architecture-resolve` exists precisely because iterating cheaply is preferred to overriding aggressively. An architect who reaches for `accept-as-is` or `supersede` early is producing a kind of false negative for the system: stuck tasks become merged-but-wrong tasks.
- **When the gate's complaint is substantive, the architect must address the substance.** "The reviewer was nitpicking" / "the validator is brittle" / "the spec was over-ambitious" are conclusions the architect may legitimately reach, but only after specifically comparing the gate's complaint against the diff and the task's intent block. The prompt instructs this comparison; reviewer/operator audits of architect verdicts should confirm the comparison happened.

These guardrails are encoded in the role-architect prompt (`services/api/treadmill_api/starters.py`). They are a load-bearing part of this ADR — the verdict surface only works if the role is anchored against its own most likely failure mode.

### 1. The architect verdict surface collapses to three actionable values

| Verdict | Semantics | Existing or new |
|---|---|---|
| `accept-as-is` | Override the blocking gate; merge if CI is good. | **Existing.** Emits `review.override` / `validate.override` events; mergeability VIEW projects approved/pass; auto-merge proceeds. |
| `amend` | Naive ralph loop. Emit a remediation hint; wf-feedback runs with it as guidance; iteration continues. | **Existing.** Routes via `maybe_dispatch_feedback_on_architect_amend`. |
| `supersede` | The plan wasn't up to snuff. Close the existing PR, create a CHILD task row with rewritten description (+ `parent_task_id` pointing back), dispatch fresh wf-author against the child. | **Repurposed.** Implementation shipped in PR #181 — see `coordination/triggers.py::maybe_dispatch_supersede_on_architect_verdict`. |

The `uncertain` verdict has been **removed** (PR #179). The architect must commit to one of the three actionable verdicts; unrecognized prose now hard-fails with `ArchitectVerdictParseError` rather than silently defaulting to a no-op retry-loop.

### 2. Task text remains immutable per row; supersede creates a child task

The supersede affordance does not edit the parent task's description in place. Instead:

- A new row is created in the `tasks` table with `description = <architect's rewritten text>` and `parent_task_id = <original task id>` pointing back.
- The child inherits `plan_id`, `repo`, `workflow_version_id` from the parent.
- The child's `title` is suffixed `" (superseded)"` so operator-facing surfaces can disambiguate.
- A fresh wf-author run is dispatched against the child.
- The parent's PR (if any) is closed best-effort. PR-close failures are logged and swallowed — the child task proceeds.

Side benefit: the immutability invariant on task descriptions stays intact across this affordance. No `task_versions` table needed. Lineage is queryable via `parent_task_id`.

### 3. The architect's escalation order is `amend` → `supersede` → (operator)

In order of preference, when the architect runs:

1. **Naive ralph loop** — if the current task spec looks well-formed and the failure looks like the worker can iterate to convergence, emit `amend` with a remediation hint. The existing ralph loop carries it.
2. **Supersede the task** — if the worker has tried and reproducibly produced no useful diff (or the spec is the visible problem), emit `supersede` with a `rewritten_description` that fixes the spec. The system creates a child task and restarts fresh on a new branch.
3. **Surface to operator** — if `wf-architecture-resolve` itself hits its dispatch cap (5 per ADR-0029 Q29.e), the system has exhausted its automated escalations. The class becomes `arch-cap-reached` in the [dead-end catalog](../diagrams/task-flow-dead-ends.md); operator notification is the next move.

The architect picks per-task based on `reasoning` it generates from the task spec + branch state + prior workflow runs. There is no separate verdict for "naive ralph" vs "give feedback" — `amend` carries the hint that drives the next iteration.

### 4. Wf-author failure shapes route by signature, not uniformly

ADR-0037 widened wf-feedback dispatch to fire on every wf-author `step.failed`. That conflates three operationally-distinct failure shapes. Going forward, wf-author terminals route as follows:

| wf-author terminal | Routes to | Why |
|---|---|---|
| `step.completed` decision=pass with PR | wf-validate + wf-review (existing) | Standard gate-evaluation. |
| `step.completed` decision=fail | wf-feedback (existing) | Author concluded the work failed; feedback role examines the failure signal. |
| `step.failed` from worker crash / OOM / SIGKILL | (none — SQS redelivers) | Per the queue-hygiene contract verified in PR #180; the worker never acks on uncaught exception. |
| `step.failed` from author-validations rejection | wf-feedback | Validations are deterministic; feedback can look at the rejection signal. |
| `step.failed` from `CodeAuthorError("no changes")` | **wf-architecture-resolve** (NEW route) | No PR exists; nothing for wf-feedback to look at. Architect-on-plan instead. |
| `step.failed` from remote rejection on `git push` | **wf-architecture-resolve** (NEW route) | Branch state is unsalvageable; architect will almost always emit `supersede`. |

Implementation of the two new routes is **out of scope for this ADR**'s shipped work and is captured in the [implementation plan](../plans/2026-05-19-architect-widening-implementation.md). This ADR documents the architecture; the plan tracks remaining wiring.

### 5. Queue hygiene is the recovery mechanism for crashed gate workers

When a wf-validate or wf-review worker crashes (process died, OOM, network failure before completion), Treadmill's recovery is **SQS redelivery**, not an architectural retry mechanism. Per PR #180:

- The worker's runner loop calls `sqs.delete_message` only on the success path inside the try block.
- An uncaught exception in the work path re-raises without acking.
- SQS's visibility-timeout machinery handles the rest: the message becomes visible again after the timeout, another worker (or the same worker after restart) picks it up.

There is no `validate-crash-no-retry` or `review-crash-no-retry` class as long as this contract is upheld. The regression tests added in PR #180 pin specific exception types (CodeAuthorError, ClientError, CalledProcessError) so a future refactor can't silently break the contract.

## Why deterministic, not LLM-judged

The new routing decisions in §4 are made on observable state (workflow id, step status, error type), not by asking an LLM "is this an impasse?" Per ADR-0047: deterministic state-based triggers in the auto-merge path, LLM judgments only at the recovery step itself (which is the architect's role).

## Why repurpose `supersede` rather than add a new verdict

The pre-ADR-0048 worker disposition emitted `dispatch.workflow_id="wf-doc-amend"` for the `supersede` verdict, intended to author a "superseding ADR" — but no API-side consumer ever picked that up, so the verdict was effectively dead. Repurposing rather than adding a new verdict:

- Keeps the schema small (three values, not four).
- Avoids a deprecation cycle that would mean two verdict names with overlapping semantics for some period.
- Matches the intuition: "supersede" reads naturally as "this version of the task is being replaced by a new version."

## Open questions for follow-up ADRs

- **Per-task supersede cap.** Today supersede falls under `wf-architecture-resolve`'s 5-attempt cap. Is that the right cap, or should supersede have its own (likely smaller — re-spec-ing a task more than 2-3 times suggests the plan is broken, not the spec)?
- **Lineage depth.** When a child task itself supersedes (grandchild), `parent_task_id` forms a chain. Do we want a depth limit, or surface a "lineage too deep" operator warning?
- **Architect's input context.** §3 says the architect picks based on task spec + branch state + prior workflow runs. What's the actual prompt shape that conveys the prior runs concisely? Today the architect sees the most-recent gate verdicts; the supersede decision likely needs more historical context. Worth its own ADR once we observe a few supersedes in practice.

## Consequences

**Positive:**
- The largest dead-end class (`author-no-diff`, ~46% of the audit) gets a real recovery path.
- Task description immutability is preserved across the rewrite affordance.
- The architect's role becomes consistent: "decide how to recover from any non-mergeable terminal," not the ad-hoc collection of triggers it was becoming.
- The supersede affordance is reusable: future trigger sources (remote-rejection on push, operator-initiated reset) can route through it without separate plumbing.

**Negative / to watch:**
- A supersede creates a fresh task row, so the audit/observability story has to follow `parent_task_id` chains rather than treating tasks as flat. Operator-facing dashboards need to handle this.
- If the architect emits `supersede` too aggressively, the loop could thrash on creating new tasks. The cap on `wf-architecture-resolve` is the safety; we need to watch real telemetry.
- The implementation shipped without a full integration test (worker dispatches supersede → child task created → fresh wf-author runs to completion). PR #181's tests cover the trigger-level mechanics; the end-to-end smoke is on the followup list.

## Implementation status

| Component | Shipped | PR |
|---|---|---|
| Remove `uncertain` from verdict enum | ✅ | #179 |
| Verify queue-hygiene contract (validate/review crash → redelivery) | ✅ | #180 |
| Migration: `tasks.parent_task_id` column | ✅ | #181 |
| `ArchitectVerdict.rewritten_description` envelope field | ✅ | #181 |
| Role-architect prompt update | ✅ | #181 |
| Worker disposition surface for supersede | ✅ | #181 |
| `maybe_dispatch_supersede_on_architect_verdict` API trigger | ✅ | #181 |
| PR-close + child-task-create + fresh wf-author dispatch | ✅ | #181 |
| Wire wf-author no-diff `step.failed` to wf-architecture-resolve | ⏳ | (plan) |
| Wire wf-author remote-rejection `step.failed` to wf-architecture-resolve | ⏳ | (plan) |
| Integration test: end-to-end supersede smoke | ⏳ | (plan) |
| Operator surface for `arch-cap-reached` | ⏳ | (plan) |

See [`docs/plans/2026-05-19-architect-widening-implementation.md`](../plans/2026-05-19-architect-widening-implementation.md) for the remaining work.
