# ADR-0056: Prompt tuning is role-agnostic via pluggable metrics

- **Status:** proposed
- **Date:** 2026-05-27
- **Related:** ADR-0053 (agentic judge-prompt optimization via workers), ADR-0041 (amended)
- **Amends:** ADR-0053 (widens scope from judges to all role types)

## Context

ADR-0053 wired up an agentic prompt optimizer (`role-prompt-optimizer` +
`wf-tune-judge-prompts`) that improves a **judge** role's prompt by scoring
verdicts against a labeled gold corpus (`evaluate_judge_prompt` in
`workers/agent/treadmill_agent/judge_eval.py`).

But the scoring mechanism — verdict-equality against gold labels — only
applies to **judge** roles (architect, reviewer, validator). The system has
other role types whose prompts ALSO shape loop count + worker burn:

- **Author roles** (`role-code-author`, `role-doc-author`) emit code/docs.
  Their "correctness" has no gold label — only a downstream signal: did the
  authored output ride cleanly to merge, or trigger wf-feedback loops?
- **Procedural roles** (`role-analyzer`, `role-triage`, `role-action`) drive
  the wf-feedback recovery cycle itself. When they're bad, they amplify
  loops instead of resolving them (the analyzer "Perfect! Complete." pattern
  this session repeatedly hit).

Direct evidence from 2026-05-26 night: **4 tasks on a single downstream
repo ran 11 times each with identical trigger profiles** (1 author-fail + 4
architect-amend + 5 wf-feedback-validation-fail + 1 registered) before the
wf-feedback 5-cap escalated them. The cap *worked* — it bounded the loop —
but the loops themselves are the artifact of role-architect's verdicts. The
judge-prompt optimizer (ADR-0053) directly targets that, but ONLY architect/
reviewer/validator. Author and procedural roles can drive the same loop
pathology and aren't optimizable by the current design.

## Decision

**Generalize the optimizer from judge-only to role-agnostic** by introducing
a pluggable metric per role type:

1. **Judge roles** keep `evaluate_judge_prompt(prompt, examples, model)` —
   verdict equality vs gold (already shipped).
2. **Author + procedural roles** get `evaluate_role_retrospectively(
   role_id, window)` — uses runtime data (`workflow_run_steps`,
   `workflow_runs.trigger`, the new `token_usage` columns) to score against
   downstream outcomes: tasks the role touched, fraction that merged clean
   (≤3 runs), fraction that hit wf-feedback ≥1 time, mean tokens burned per
   touched task.
3. The optimizer ROLE PROMPT (`role-prompt-optimizer`) detects which metric
   applies based on the target role's type and uses the right scorer. The
   role prompt itself stays simple — it dispatches to the right scoring
   function rather than knowing each role's internals.
4. The workflow renames `wf-tune-judge-prompts` → `wf-tune-role-prompts`
   (additive: keep the old slug aliased to preserve the existing schedule
   row for backward compatibility, with a docstring deprecation note).
5. The cron schedule rotates through roles (or fires per-role on
   stagger). Initially: weekly stagger — Saturday role-architect, Sunday
   role-code-author, etc.; payload's `role_id` parameterizes which role to
   tune.

### Why pluggable, not separate workflows

A separate `wf-tune-author-prompts` would duplicate ~all the optimizer's
machinery (propose variant → score → emit PR) just to swap the scorer. The
scoring function is the only divergence; everything else is shared. One
optimizer + one workflow keeps the surface small and avoids drift between
"judge tuning" and "role tuning."

### Retrospective signal — what it actually measures

For non-judge roles, the optimizer can't compare to gold (none exists). But
the system already collects the signal we want: when role-X authored or
processed a step in a task, what happened downstream? Concretely:

```
SELECT
  t.id,
  count(*) FILTER (WHERE wr.trigger LIKE 'self:wf-feedback-%') as feedback_runs,
  count(*) FILTER (WHERE wr.trigger LIKE 'self:architect-amend') as amend_runs,
  bool_or(EXISTS(SELECT 1 FROM events e WHERE e.task_id=t.id AND e.action='pr_merged')) as merged,
  sum(s.input_tokens + s.output_tokens) as tokens
FROM tasks t
JOIN workflow_run_steps s ON ... JOIN workflow_runs wr ON ...
WHERE s.role_id = '<role-to-evaluate>' AND s.completed_at > now() - interval '<window>'
GROUP BY t.id;
```

A "clean run" = merged AND ≤3 total runs. A "looped" run = ≥1 wf-feedback.
The score is `clean_fraction - 0.5 * looped_fraction` (penalize loops more
than rewards clean) on the population the role touched during the window.

When we propose a variant prompt, we can't *score it directly* against past
data (the past used the OLD prompt). So we score retrospectively: emit a
**canary** — push the variant on a small fraction of upcoming dispatches via
a feature flag (10% sampling for ~24h), then compare. If the variant's
canary cohort outscores the control, promote.

(For the first wave we can defer the canary mechanism and just score the
CURRENT prompt retrospectively + propose a variant + let the operator
review whether the rationale is sound. Canary-vs-control is Wave 2.)

## Consequences

**Pros:**
- One optimizer surface covers all role types — same machinery, same review
  cycle, same operator interface.
- Reduces "architect over-rejects downstream-repo tasks → 11-run loops" by
  closing the feedback loop (architect's verdicts → retrospective measure →
  prompt tuning).
- Naturally extends to NEW role types (e.g., the future operator-alert
  role): just register the right scorer.

**Cons / open:**
- Retrospective scoring relies on enough recent task volume per role. Low-
  volume roles (e.g., one-off doc roles) won't have statistical power for
  variant ranking. Mitigation: lower the optimization frequency for those
  roles (the schedule's per-role stagger handles this naturally), or fall
  back to a synthetic-eval approach.
- Canary-vs-control is non-trivial (feature flag plumbing, sampling). Can
  defer to Wave 2 of this design.
- The role-prompt-optimizer prompt grows — but only in routing logic;
  the underlying eval functions stay separate + testable.

## Implementation outline

See `docs/plans/2026-05-27-wave4-prompt-tuning-role-agnostic.md` for the
work breakdown.

## Decisions captured during execution

_(filled when the plan completes)_
