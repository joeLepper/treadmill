---
status: active
---

# Plan: ADR-0040 architect-tunes-validator implementation

- **Status:** active
- **Date:** 2026-05-16
- **Related ADRs:** ADR-0040 (parent), ADR-0006 (rules YAML format), ADR-0032 (role-architect), ADR-0036 (severity gating), ADR-0038 (deadlock arbitration)

## Goal

Close the per-incident-override loop into compounding validator improvement. When the architect verdicts `accept-as-is` on a validate-fail-driven deadlock, the system produces a reviewable rule-tuning PR alongside the existing `review.override` event. Operator merges the tuning PR; the rule corpus converges toward fewer false positives; architect dispatches trend down over time.

## Success criteria

- `ValidatorTuning` Pydantic envelope exists at `services/api/treadmill_api/events/validator_tuning.py` with fields `rule_slug`, `check_id`, `action` (one of `demote_severity` / `narrow_applies_to` / `refine_prompt`), `evidence`, `proposed_patch`. Tests reject malformed shapes.
- `role-architect`'s system_prompt teaches it to emit a `validator_tuning` proposal in its output payload when verdict is `accept-as-is` AND the deadlock trigger was `wf-validate.decision='fail'`. Trigger is detectable from the dispatch context's wf-validate run id.
- Architect disposition (`workers/agent/treadmill_agent/runner_dispositions/architecture.py`) parses the tuning proposal and surfaces it as `payload.validator_tuning`.
- Coordination consumer's new helper `maybe_dispatch_rule_tuning_on_architect_completion` fires `wf-doc-amend` with the new intent literal `tune-rule-from-architect` and the proposal as task_directive context.
- `role-documentarian`'s system_prompt handles the new intent: read the proposal, edit the rule YAML, commit per ADR-0033, open the PR. PR title pattern: `Tune rule: <rule-slug> (<action>)`.
- One end-to-end smoke against the live system: induce a `wf-validate.fail` deadlock on a fixture task, run the architect, observe both the `review.override` AND the `wf-doc-amend`-dispatched rule-tuning PR.
- Dedup namespace `wf-doc-amend:<repo>:tune-rule=<rule-slug>` prevents duplicate tuning PRs against the same rule within a deduplication window.

## Constraints / scope

### In scope
- The 7 tasks below, in dependency order.
- Tuning PRs are reviewable, not auto-merged. The plan opts out of ADR-0031 auto-merge for the rule-tuning PRs specifically (via the wf-doc-amend dispatch's plan-frontmatter inheritance — or equivalently, by routing through a plan that sets `auto_merge: false`).

### Out of scope
- `refine_prompt` validation/sanity-checking. We trust the architect's proposed patch and rely on operator review at PR time.
- Cap-based tuning escalation. If the architect proposes the same rule's `refine_prompt` 10 times in a week, we don't yet detect or escalate — that's an ADR-0040 follow-up.
- Crystallization integration. ADR-0034 (learnings → rules) is the inbound flow; this plan is the inbound-correction flow. Both touch `docs/knowledge-base/rules/` but their loops don't overlap in v1.
- Automated severity-decay measurement (how do we know the tuning worked?). The signal is qualitative for now: architect dispatch frequency over time.
- ADR-0006 schema changes. The tuning actions all fit the existing rule YAML; no new fields required.

### Budget
Two operator-attended sessions for review + dispatch + smoke validation. If the work overflows two sessions, we abort and post-mortem rather than escalate. Budget for live testing: ~$50 in Claude spend across smoke iterations.

## Sequence of work

```yaml
sequence_of_work:
  - id: validator-tuning-schema
    title: ValidatorTuning Pydantic envelope + registry registration
    workflow: wf-author
    intent: |
      Author the Pydantic model ``ValidatorTuning`` at
      ``services/api/treadmill_api/events/validator_tuning.py`` with
      these fields:

        rule_slug: str  # 'implementation-conforms-to-diagram'
        check_id: str   # the specific check within the rule
        action: Literal["demote_severity", "narrow_applies_to", "refine_prompt"]
        evidence: str   # one-paragraph spec-vs-diff evidence
        proposed_patch: dict[str, Any]
          # action-shape-specific:
          # demote_severity: {"from": "blocking", "to": "warning"|"advisory"}
          # narrow_applies_to: {"remove_globs": [str], "keep_globs": [str]}
          # refine_prompt: {"diff_text": str}  # natural-language diff

      Re-export from events/__init__.py + events/registry.py. Tests:
      validate well-formed JSON; reject missing fields; reject invalid
      action literal; validate proposed_patch shape per action.
    scope:
      files:
        - services/api/treadmill_api/events/validator_tuning.py
        - services/api/treadmill_api/events/__init__.py
        - services/api/treadmill_api/events/registry.py
        - services/api/tests/test_validator_tuning.py
    validation:
      - kind: deterministic
        description: Schema module exists; round-trip + rejection tests pass.
        script: |
          cd services/api \
            && test -f treadmill_api/events/validator_tuning.py \
            && grep -q "class ValidatorTuning" treadmill_api/events/validator_tuning.py \
            && uv run pytest tests/test_validator_tuning.py -q

  - id: architect-prompt-emits-tuning
    title: Update role-architect prompt to emit validator_tuning on validate-fail accept-as-is
    workflow: wf-author
    depends_on:
      - task.validator-tuning-schema.pr_merged
    intent: |
      Extend ``role-architect``'s system_prompt in
      ``services/api/treadmill_api/starters.py`` so that, when the
      deadlock trigger was wf-validate.fail (detectable from the
      dispatch context's mention of validator + check), the architect's
      JSON envelope includes a ``validator_tuning`` field alongside the
      existing ``verdict`` / ``reasoning`` / ``target_artifact``.

      Prompt teaches the three actions + their proposed_patch shapes
      with examples. Bias: prefer demote_severity over the others when
      uncertain (less invasive). Prefer narrow_applies_to when the rule
      fires on shapes it shouldn't. Reserve refine_prompt for when the
      check's LLM-judge prompt itself is the problem.

      Update test_starters.py to assert the prompt mentions
      ``validator_tuning``, the three action literals, and the rule
      slug field.
    scope:
      files:
        - services/api/treadmill_api/starters.py
        - services/api/tests/test_starters.py
    validation:
      - kind: deterministic
        description: Prompt mentions validator_tuning + three actions; tests pass.
        script: |
          cd services/api \
            && grep -q "validator_tuning" treadmill_api/starters.py \
            && grep -q "demote_severity" treadmill_api/starters.py \
            && grep -q "narrow_applies_to" treadmill_api/starters.py \
            && grep -q "refine_prompt" treadmill_api/starters.py \
            && uv run pytest tests/test_starters.py -q

  - id: architecture-disposition-surfaces-tuning
    title: Architect disposition parses + surfaces validator_tuning payload
    workflow: wf-author
    depends_on:
      - task.architect-prompt-emits-tuning.pr_merged
    intent: |
      Extend
      ``workers/agent/treadmill_agent/runner_dispositions/architecture.py``
      so the parsed envelope's optional ``validator_tuning`` sub-object
      gets surfaced as ``StepOutput.payload.validator_tuning``. Validate
      it via the ValidatorTuning Pydantic model when present; raise on
      malformed (or drop with WARN log — pick the gentler option since
      this is best-effort).

      Tests: envelope with valid tuning → surfaces. Envelope without →
      no key. Envelope with malformed tuning → graceful drop + log.
    scope:
      files:
        - workers/agent/treadmill_agent/runner_dispositions/architecture.py
        - workers/agent/tests/test_runner_dispositions.py
    validation:
      - kind: deterministic
        description: Disposition surfaces validator_tuning + tests pass.
        script: |
          cd workers/agent \
            && grep -q "validator_tuning" treadmill_agent/runner_dispositions/architecture.py \
            && uv run pytest tests/test_runner_dispositions.py -q -k "architect"

  - id: consumer-dispatch-rule-tuning
    title: Consumer trigger fires wf-doc-amend on architect.validator_tuning
    workflow: wf-author
    depends_on:
      - task.architecture-disposition-surfaces-tuning.pr_merged
    intent: |
      Author
      ``maybe_dispatch_rule_tuning_on_architect_completion`` in
      ``services/api/treadmill_api/coordination/triggers.py``. Fires on
      wf-architecture-resolve.step.completed where
      payload.validator_tuning is present. Dispatches wf-doc-amend with
      a payload carrying the tuning proposal + new intent literal
      ``tune-rule-from-architect``. Dedup namespace
      ``wf-doc-amend:<repo>:tune-rule=<rule-slug>``.

      Wire the call from coordination/consumer.py alongside
      _maybe_emit_review_override. Tests: helper fires on
      tuning-present; helper short-circuits on tuning-absent; dedup
      key shape correct.
    scope:
      files:
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/treadmill_api/coordination/consumer.py
        - services/api/treadmill_api/coordination/dispatch_dedup.py
        - services/api/tests/test_consumer_unit.py
    validation:
      - kind: deterministic
        description: Trigger helper + dispatch dedup wired; tests pass.
        script: |
          cd services/api \
            && grep -q "maybe_dispatch_rule_tuning_on_architect_completion" treadmill_api/coordination/triggers.py \
            && grep -q "tune-rule-from-architect" treadmill_api/coordination/triggers.py \
            && uv run pytest tests/test_consumer_unit.py tests/test_dispatch_dedup.py -q

  - id: documentarian-handles-tune-rule-intent
    title: role-documentarian handles tune-rule-from-architect intent
    workflow: wf-author
    depends_on:
      - task.consumer-dispatch-rule-tuning.pr_merged
    intent: |
      Extend ``role-documentarian``'s system_prompt to handle the new
      intent literal ``tune-rule-from-architect``. The role reads the
      tuning proposal from prior_steps[-1].output.payload.task_directive,
      applies the action to the rule YAML at
      ``docs/knowledge-base/rules/<rule-slug>.yaml``, commits per
      ADR-0033 conventions, opens the PR with title ``Tune rule:
      <rule-slug> (<action>)``. The PR body cites the architect run
      that triggered it for auditability.

      For demote_severity: edit checks[i].severity in the YAML.
      For narrow_applies_to: edit applies_to list.
      For refine_prompt: apply the natural-language diff to the
      checks[i].prompt field (the role uses the proposed_patch.diff_text
      as authoring guidance, not a literal patch).

      Tests in test_starters.py assert the prompt mentions the new
      intent + each action's edit pattern.
    scope:
      files:
        - services/api/treadmill_api/starters.py
        - services/api/tests/test_starters.py
    validation:
      - kind: deterministic
        description: Documentarian prompt mentions the new intent + patterns; tests pass.
        script: |
          cd services/api \
            && grep -q "tune-rule-from-architect" treadmill_api/starters.py \
            && uv run pytest tests/test_starters.py -q

  - id: tuning-pr-frontmatter-discipline
    title: Tuning PRs opt out of auto-merge
    workflow: wf-author
    depends_on:
      - task.documentarian-handles-tune-rule-intent.pr_merged
    intent: |
      Ensure that PRs produced by the tune-rule-from-architect path
      land with auto_merge=false (or its equivalent at PR-creation
      time). Two implementation options to evaluate:

      (a) The tune-rule dispatch creates a synthetic micro-plan with
          auto_merge: false frontmatter so its PR inherits the opt-out.
      (b) The wf-doc-amend disposition (when intent ==
          tune-rule-from-architect) writes the auto_merge=false signal
          directly into the task_prs row or skips emitting the
          auto-merge-eligible event.

      Pick whichever requires the smaller blast radius. Add a test
      that the resulting task_prs row's auto_merge field is false.
    scope:
      files:
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/tests/test_consumer_unit.py
    validation:
      - kind: deterministic
        description: Tuning PRs cannot auto-merge; tests pass.
        script: |
          cd services/api && uv run pytest tests/test_consumer_unit.py -q -k "tune"

  - id: smoke-end-to-end
    title: Smoke — induce a validate-fail deadlock + observe both override and tuning PR
    workflow: wf-validate
    depends_on:
      - task.tuning-pr-frontmatter-discipline.pr_merged
    intent: |
      Operator end-to-end: pick a fixture task that will trip the
      implementation-conforms-to-diagram rule on a clean PR (no
      DIAGRAM_SOURCE available so the judge fires defensively). Run
      the full pipeline against the live system. Observe:

        1. wf-author produces PR.
        2. wf-validate decision=fail on implementation-conforms.
        3. wf-feedback decision=responded-without-change.
        4. Architect dispatch via ADR-0038 widened predicate.
        5. Architect verdict=accept-as-is + validator_tuning proposal.
        6. review.override event emitted; mergeability flips to
           mergeable.
        7. wf-doc-amend dispatched with intent=tune-rule-from-architect;
           opens a tuning PR against the rule YAML.
        8. Tuning PR does NOT auto-merge; awaits operator review.

      Document the cycle in
      docs/handoffs/2026-05-XX-architect-tunes-validator-first-smoke.md.
      Include token spend + screenshots of the dual artifacts (override
      event row + tuning PR).
    scope:
      files:
        - docs/handoffs/2026-05-XX-architect-tunes-validator-first-smoke.md
    validation:
      - kind: deterministic
        description: Handoff doc exists; names the architect run + tuning PR.
        script: |
          ls docs/handoffs/2026-05-*-architect-tunes-validator-first-smoke.md \
            && grep -qE "architect run [a-f0-9-]+" docs/handoffs/2026-05-*-architect-tunes-validator-first-smoke.md \
            && grep -qE "tuning PR #[0-9]+" docs/handoffs/2026-05-*-architect-tunes-validator-first-smoke.md
```

## Diagram

See ADR-0040 §Diagram for the operator → architect → documentarian → operator flow with the new parallel branch (review.override + validator_tuning).

## Risks / unknowns

- **The architect's tuning judgment is wrong sometimes.** Operator review on the tuning PR is the safety net. We'll abort the plan if more than 50% of tuning PRs over the first 10 are rejected at operator review — that signals the architect's tuning prompt needs major work before this is useful.
- **Tuning the prompt for the implementation-conforms-to-diagram rule (its first likely target) is harder than tuning severity or applies_to.** Mitigation: bias the architect's prompt toward demote_severity / narrow_applies_to over refine_prompt; refine_prompt is the escape hatch, not the default.
- **Duplicate tuning proposals across runs.** Dedup namespace `wf-doc-amend:<repo>:tune-rule=<rule-slug>` blocks within-window duplicates. Across the deduplication TTL the same rule could be tuned twice with conflicting patches; operator notices at review.
- **The plan's seven tasks form a strict chain.** If task 4 (consumer dispatch) reveals architectural surprises, tasks 5–7 stall. Mitigation: drop the strict chain to logical chain only — tasks 1–3 can be authored in parallel; tasks 5+6 can be authored in parallel after 4. The sequence_of_work above expresses the strictest read.

## Decisions captured during execution

(none yet)

## Post-mortem

(filled when status moves to `completed` or `abandoned`)
