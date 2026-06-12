# ADR-0090: Wake-filter the evaluator + decouple coordinator CI-observation

- **Status:** proposed
- **Date:** 2026-06-12
- **Related:** ADR-0089 (token-economics wake filtering — this extends its
  per-role defaults), ADR-0087 (team execution model), ADR-0068
  (treadmill-events channel), ADR-0071 (per-session relay levels)

## Context

After ADR-0089 (orchestrator wake filtering) and the worker→sonnet tier
switch (2026-06-12), the fleet still hit the Claude subscription 5-hour
limit three times in the 2026-06-11→12 window. With workers now on a cheap
tier, the dominant remaining burn is the **opus** sessions — and within
them, the highest-volume wake class is `github.check_run_completed`
(~13 per PR) plus `github.pr_synchronize`, each re-reading a large context.

ADR-0089 deliberately left `coordinator` / `evaluator` **unfiltered**
("their bookkeeping consumes the noisy classes today") — a coarse caution
taken before we'd derived what each role actually acts on. Inspection of the
templates now shows the precise picture:

- The **coordinator** is the CI-**observer**: its §3 handlers include
  `check_run.completed`, and it *emits* the `task.ci_result` rollup
  (`tools/team-templates/coordinator/CLAUDE.md.tmpl:155`) that everyone else
  reads. It genuinely depends on the raw, noisy class.
- The **evaluator** keys off the `task.ci_result` rollup + peer/evaluator
  verdicts + review handoffs. It does **not** consume raw `check_run`s.

So the original "they all need it" is wrong: only the coordinator-as-observer
needs `check_run.completed`. The wake-filter mechanism is already
env/role-default driven (`TREADMILL_WAKE_ACTIONS`, ADR-0089) — turning on a
filter for any role is configuration, not new machinery.

## Decision

### 1. Evaluator wake allowlist (config only)

Give the `evaluator` role a default wake allowlist: `task.ci_result`,
`task.*_verdict`, the review-handoff/assignment actions it acts on,
`task.escalat*` + the enumerated escalation-class actions, and the
plan/task lifecycle it reacts to — **excluding** `github.check_run_completed`
and `github.pr_synchronize`. Same `wake ⊇ relay` invariant, suppression
digest, and max-suppression-age self-wake as ADR-0089. No new mechanism.

### 2. Decouple CI-observation from the coordinator's LLM wake path

Move suite-completion detection off the LLM coordinator. A lightweight
**non-LLM observer** consumes `github.check_run_completed`, computes
check-suite completion, and writes the `task.ci_result` event — the same
rollup the coordinator emits today, now produced mechanically. The
coordinator's wake allowlist then **excludes** raw `check_run.completed` and
**includes** `task.ci_result`. Net effect: a PR's ~13 `check_run` wakes
collapse to **one** `ci_result` wake for the coordinator. The coordinator's
§3 `check_run.completed` handler is replaced by a `task.ci_result` handler.

### 3. Forbidden-failure-mode guard (unchanged from ADR-0089)

Every escalation-class and terminal-decision action stays in both new
allowlists, enumerated by name where it escapes a glob. A filtered-away
escalation is the one failure mode this design must never have.

## Consequences

- The largest remaining opus burn (coordinator + evaluator wake volume) is
  cut substantially while **teams keep running in parallel** — no
  serialization (that is ADR-0091's lever, held as backstop).
- `ci_result` emission moves from the LLM coordinator to a mechanical
  observer: more reliable and not subject to LLM cadence/availability.
- Risk: the non-LLM observer must compute suite completion exactly as the
  coordinator does today (multi-suite, reruns, the netlify-vs-CI distinction
  seen 2026-06-12). Pin it with tests against captured check-suite payloads.
- Risk: an under-broad evaluator allowlist could drop a review-handoff wake
  and stall reviews — derive it from the evaluator template's actual
  handlers and verify before shipping.

## Alternatives

- **Incumbent — leave coordinator/evaluator unfiltered (ADR-0089's choice).**
  This is what runs today. Rejected as the end state: it is the dominant
  remaining burn after sonnet, and we have now identified that only the
  coordinator-as-observer needs the noisy class — the original blanket
  caution is too coarse to justify paying for every evaluator's check_run
  re-reads.
- **Filter the coordinator naively (drop `check_run` with no observer).**
  Rejected: it blinds CI tracking — the coordinator *is* the observer that
  produces `ci_result`.
- **Serial team execution (ADR-0091) instead.** Complementary, not
  exclusive: a bigger lever but more build and it serializes teams. This ADR
  is the parallel-preserving first move; ADR-0091 is the backstop if it is
  not enough.

## Out of scope

- The team-queueing scheduler (ADR-0091).
- Model routing / further tiering.
- Per-call metering (ADR-0089 §2).
- Filtering `worker` sessions (their consumption is unmeasured; defer).
