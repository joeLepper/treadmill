---
status: drafting
trigger: ADR-0034 accepted 2026-05-14. Drafted same-day. **Held — DO NOT submit via CLI** until ADR-0031 hands-free driving lands per memory/feedback_dont_compound_during_migration.md.
parent: docs/adrs/0034-learnings-to-rules-crystallization.md
---

# Plan: Learnings-to-rules crystallization (ADR-0034 execution)

Ship the wf-crystallize-learning workflow + readiness judge + role-architect crystallization + `treadmill learnings crystallize` CLI. Closes ADR-0001 opinion #3 with machinery.

## Goal

After execution: an operator runs `treadmill learnings crystallize`, the system iterates `docs/learnings/*.md` with `status: captured`, judges readiness against ADR-0034's frequency × ease-of-deterministic-remediation criteria, dispatches role-architect to author the rule YAML + matching check.sh for `ready` verdicts, updates the learning's status to `crystallized-into-rule-<slug>`, and leaves `not-ready` learnings annotated with backoff state.

## Success criteria

- `services/api/treadmill_api/events/crystallization_verdict.py` defines the `CrystallizationVerdict` Pydantic envelope per ADR-0034.
- `services/api/treadmill_api/starters.py` carries `role-crystallization-judge` (output_kind=`analysis`) + the `wf-crystallize-learning` two-step workflow (judge → architect). `test_starters.py` asserts both.
- `workers/agent/treadmill_agent/runner_dispositions/crystallization.py` (or shared with `architecture.py`) handles the workflow: parses verdict, dispatches step 2 on `ready`, updates frontmatter + backoff on `not-ready` / `defer`.
- A new `cli/treadmill_cli/commands/learnings.py` exposes `treadmill learnings crystallize` — single command that fans out (per Q34.c).
- Smoke: one captured learning (pick `2026-05-14-authors-must-run-validation-before-submitting.md`) goes through end-to-end; verdict=ready; rule YAML produced at `docs/knowledge-base/rules/<slug>.yaml`; learning's `status:` flipped to `crystallized-into-rule-<slug>`.

## Constraints / scope

### In scope

- 6 tasks below.
- v1 operator-dispatched only; periodic dispatch deferred to ADR-0035 scheduler + its plan.

### Out of scope

- Periodic dispatch (awaits ADR-0035 scheduler).
- Learning-status hygiene under rule supersession (Q34.f deferred — v1 holds the initial status).
- Auto-merge of crystallization PRs — those land like any other PR, gated by wf-validate.

### Budget

One operator session for review + dispatch + smoke. **NOT dispatched until hands-free** per session discipline.

## Diagram

See ADR-0034 §Diagram for the operator → judge → architect → repo → status-flip flow.

## Risks / unknowns

- **Judge mis-classifies readiness** (false positives produce noise rules; false negatives leave gaps un-enforced). Mitigation: `not-ready` verdicts are reversible by editing the learning's `proposed rule` and re-firing. False positives caught at PR-review on the generated rule.
- **Architect fails to find the right check.sh template** when proposed remediation is deterministic. Mitigation: architect's prompt enumerates the existing rule patterns (grep / pytest / cdk synth / etc.) and biases toward the closest match.
- **Backoff state in frontmatter conflicts** with operator-mediated learning edits. Mitigation: backoff fields are namespaced (`crystallization_backoff_until` etc.) and don't collide with the existing schema.

## Sequence of work

```yaml
sequence_of_work:
  - id: crystallization-verdict-schema
    title: CrystallizationVerdict Pydantic envelope
    workflow: wf-author
    intent: |
      Author the Pydantic model ``CrystallizationVerdict`` at
      ``services/api/treadmill_api/events/crystallization_verdict.py``
      mirroring ADR-0032's ``ArchitectVerdict``:

        verdict: Literal["ready", "not-ready", "defer"]
        reasoning: str
        learning_slug: str
        proposed_rule_slug: str | None = None

      Re-export from
      ``services/api/treadmill_api/events/__init__.py``.

      Tests: validate well-formed JSON; reject missing required
      fields; reject invalid verdict literal.
    scope:
      files:
        - services/api/treadmill_api/events/crystallization_verdict.py
        - services/api/treadmill_api/events/__init__.py
        - services/api/tests/test_crystallization_verdict.py
    validation:
      - kind: deterministic
        description: |
          Schema module exists; round-trip works.
        script: |
          cd services/api \
            && test -f treadmill_api/events/crystallization_verdict.py \
            && grep -q "class CrystallizationVerdict" treadmill_api/events/crystallization_verdict.py \
            && grep -q '"ready"' treadmill_api/events/crystallization_verdict.py \
            && grep -q '"not-ready"' treadmill_api/events/crystallization_verdict.py \
            && grep -q '"defer"' treadmill_api/events/crystallization_verdict.py \
            && uv run pytest tests/test_crystallization_verdict.py -q

  - id: judge-role-and-workflow
    title: role-crystallization-judge + wf-crystallize-learning in starters.py
    workflow: wf-author
    depends_on:
      - task.crystallization-verdict-schema.pr_merged
    intent: |
      Author ``role-crystallization-judge`` in
      ``services/api/treadmill_api/starters.py``:
        - output_kind: ``analysis``
        - model: WORKER_MODEL (haiku per ADR-0029 Q29.b; rules
          override later if needed)
        - system_prompt: reads the candidate learning + recent
          counts of similar incidents in
          ``docs/learnings/*.md`` and PR comments; weighs
          frequency × ease-of-deterministic-remediation per
          ADR-0034 Q34.b. Returns ``CrystallizationVerdict`` JSON
          envelope.

      Define ``wf-crystallize-learning`` workflow with two steps:
      step 1 → role-crystallization-judge; step 2 → role-architect
      (gated on judge verdict via disposition routing).

      Update test_starters.py.
    scope:
      files:
        - services/api/treadmill_api/starters.py
        - services/api/tests/test_starters.py
    validation:
      - kind: deterministic
        description: |
          starters has the role + workflow; tests pass.
        script: |
          cd services/api && uv run pytest tests/test_starters.py -q \
            && grep -q "role-crystallization-judge" treadmill_api/starters.py \
            && grep -q "wf-crystallize-learning" treadmill_api/starters.py

  - id: crystallization-disposition
    title: workers crystallization.py handles wf-crystallize-learning
    workflow: wf-author
    depends_on:
      - task.judge-role-and-workflow.pr_merged
    intent: |
      Author
      ``workers/agent/treadmill_agent/runner_dispositions/crystallization.py``
      (or fold into existing ``architecture.py`` if shapes share).

      Step-1 disposition (judge output):
        - Parse step output as ``CrystallizationVerdict`` envelope.
        - On ``ready``: dispatch step 2 with the learning slug +
          rule slug payload.
        - On ``not-ready``: update the learning's frontmatter with
          ``last_crystallization_check`` + exponential backoff
          (1d, 3d, 7d, 14d, 30d). Capture rationale in the
          learning's Notes section.
        - On ``defer``: no-op (re-evaluated on next crystallize
          run).

      Step-2 disposition (architect output): architect produces
      the rule YAML + check.sh. Disposition writes them to
      ``docs/knowledge-base/rules/<slug>.yaml`` +
      ``tools/rule-checks/<slug>/check.sh``, updates the source
      learning's ``status:`` to ``crystallized-into-rule-<slug>``,
      opens a PR.

      Tests parametrized over the three verdicts + the
      architect step's downstream effects.
    scope:
      files:
        - workers/agent/treadmill_agent/runner_dispositions/crystallization.py
        - workers/agent/treadmill_agent/runner.py
        - workers/agent/tests/test_runner_dispositions.py
    validation:
      - kind: deterministic
        description: |
          Disposition module + tests pass.
        script: |
          cd workers/agent \
            && test -f treadmill_agent/runner_dispositions/crystallization.py \
            && grep -q "def handle" treadmill_agent/runner_dispositions/crystallization.py \
            && grep -q "CrystallizationVerdict" treadmill_agent/runner_dispositions/crystallization.py \
            && grep -qE "ready|not-ready|defer" treadmill_agent/runner_dispositions/crystallization.py \
            && uv run pytest tests/test_runner_dispositions.py -q -k crystalliz

  - id: cli-crystallize-command
    title: treadmill learnings crystallize CLI command
    workflow: wf-author
    depends_on:
      - task.crystallization-disposition.pr_merged
    intent: |
      Add ``treadmill learnings crystallize`` to the CLI:

        - Scan ``docs/learnings/*.md`` for status=captured AND
          (no last_crystallization_check OR backoff_until <= now).
        - Single fan-out task dispatched to wf-crystallize-learning
          (per Q34.c — one task per CLI run, fan-out inside).
        - Print progress + verdict summary on completion.

      Tests cover: scan output excludes
      crystallized + backoff-still-active learnings; dispatch
      payload correctly carries the candidate slugs.
    scope:
      files:
        - cli/treadmill_cli/commands/learnings.py
        - cli/tests/test_learnings_command.py
    validation:
      - kind: deterministic
        description: |
          CLI command exists + tests pass.
        script: |
          cd cli && uv run pytest tests/test_learnings_command.py -q \
            && grep -q "crystallize" treadmill_cli/commands/learnings.py

  - id: rule-engine-load-newly-crystallized
    title: Rule engine picks up crystallized rules without restart
    workflow: wf-author
    depends_on:
      - task.crystallization-disposition.pr_merged
    intent: |
      Verify (and patch if needed) that the rule engine
      authored under ADR-0030 picks up new
      ``docs/knowledge-base/rules/<slug>.yaml`` files on the next
      wf-validate run without an API restart. If the engine
      caches the rule corpus, add a per-PR re-scan since rules
      land mid-session.

      Tests: synthesize a fresh rule file; trigger wf-validate;
      assert the new rule is in the evaluator's check set.
    scope:
      files:
        - services/api/treadmill_api/rules/engine.py
        - services/api/tests/test_rule_engine.py
    validation:
      - kind: deterministic
        description: |
          New rule files load without API restart.
        script: |
          cd services/api && uv run pytest tests/test_rule_engine.py -q

  - id: smoke-end-to-end
    title: Smoke — crystallize one captured learning end-to-end
    workflow: wf-validate
    depends_on:
      - task.cli-crystallize-command.pr_merged
      - task.rule-engine-load-newly-crystallized.pr_merged
    intent: |
      Operator runs ``treadmill learnings crystallize`` against
      the running deployment. Pick
      ``docs/learnings/2026-05-14-authors-must-run-validation-before-submitting.md``
      as the smoke target (it has a clear proposed rule +
      remediation).

      Expected: judge returns ``ready``; architect authors
      ``docs/knowledge-base/rules/author-side-validation-pre-push.yaml``
      + ``tools/rule-checks/author-side-validation-pre-push/check.sh``;
      learning's ``status:`` flips to
      ``crystallized-into-rule-author-side-validation-pre-push``;
      a PR opens with all three changes.

      Document the cycle + token spend in
      ``docs/handoffs/2026-05-14-crystallization-first-smoke.md``.
    scope:
      files:
        - docs/handoffs/2026-05-14-crystallization-first-smoke.md
    validation:
      - kind: deterministic
        description: |
          Handoff doc exists; names the verdict, the rule slug,
          and confirms the learning status flipped.
        script: |
          test -f docs/handoffs/2026-05-14-crystallization-first-smoke.md \
            && grep -qi "verdict" docs/handoffs/2026-05-14-crystallization-first-smoke.md \
            && grep -qi "crystallized-into-rule" docs/handoffs/2026-05-14-crystallization-first-smoke.md
```

## Decisions captured during execution

(empty)

## Post-mortem

Filled in on transition to `completed`/`abandoned`.
