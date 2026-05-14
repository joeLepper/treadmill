---
status: active
trigger: ADR-0030 parent plan (2026-05-14-adr-0030-federated-in-repo-context.md) had 4 tasks permanently blocked on a broken dep — skill-updates was operator-completed via PR #39 outside Treadmill's task_prs linkage, so its pr_merged event never fires. Trimming + re-firing the 4 stuck tasks here with deps to skill-updates dropped. backfill-followup-plan keeps its deps on the 2 rule tasks (both in this plan).
parent: docs/adrs/0030-federated-in-repo-agent-context.md
related: docs/plans/2026-05-14-adr-0030-federated-in-repo-context.md
---

# Plan: ADR-0030 — trimmed deps re-fire

Re-fire the 4 tasks from the parent plan that are stuck on the broken `skill-updates` dep. The parent plan's other 5 tasks landed normally; this plan only carries the stuck remainder.

## Goal

Unblock the four ADR-0030 tasks whose `depends_on` references `task.skill-updates.pr_merged` (an event that will never fire because the task was operator-completed via PR #39 outside Treadmill's PR-tracking).

## Success criteria

Same as the parent plan for these four tasks:

- `services/api/treadmill_api/starters.py` carries updated prompts for role-doc-author, role-code-author, role-reviewer.
- `docs/knowledge-base/rules/adr-and-plan-has-diagram.yaml` + check.sh exist; rule parses against ADR-0006 schema.
- `docs/knowledge-base/rules/implementation-conforms-to-diagram.yaml` exists; llm-judge prompt references ADR-0004's six conformance criteria + four-outcome contract.
- Follow-up backfill plan exists on disk listing ≥ 33 per-artifact tasks.

## Constraints / scope

### In scope

Re-firing exactly four tasks: role-prompt-updates, rule-adr-and-plan-has-diagram, rule-implementation-conforms-to-diagram, backfill-followup-plan. Each is verbatim from the parent plan minus any reference to `task.skill-updates.pr_merged`.

### Out of scope

- Anything already merged (skills, schema doc, AGENT.md seed, the two agent-md-seed-dependent rules).
- A new workflow shape — first cut uses existing `wf-author`.

### Budget

One operator session for review + dispatch + sweep, riding on the parent plan's budget.

## Diagram

See `docs/adrs/0030-federated-in-repo-agent-context.md` §Diagram.

## Risks / unknowns

- **role-prompt-updates may hit the same `.claude/` self-modification hesitation** as skill-updates did, since updating `treadmill_api/starters.py` involves rewriting prompts the agent itself reads from. Mitigation: task #123 captured this class; if it recurs, operator completes manually on the branch as we did for #39.
- **The two rule tasks** are tame doc/YAML additions; lower failure-class risk.

## Sequence of work

```yaml
sequence_of_work:
  - id: role-prompt-updates
    title: role-doc-author + role-code-author + role-reviewer prompts wire ADR-0030 discipline
    workflow: wf-author
    intent: |
      Update three roles' ``system_prompt`` in
      ``services/api/treadmill_api/starters.py`` (the bootstrap
      fixture per ADR-0028 — live deployments need
      ``treadmill role update`` to pick up the change; document the
      operator step in ``docs/runbooks/edit-a-role-prompt.md``).

      role-doc-author: when authoring a plan, embed a mermaid
      diagram per the diagram-type guidance in
      ``.claude/skills/plan/SKILL.md`` and verify the diagram meets
      ADR-0004's conformance checklist.

      role-code-author: BEFORE implementing, read the plan's mermaid
      diagram AND any cited ADR's mermaid diagram. Those diagrams are
      the contract of intent per ADR-0004. When the change alters a
      component's externally-visible surface, update the relevant
      component's AGENT.md.

      role-reviewer: in addition to the existing review criteria,
      flag missing diagrams + stale AGENT.md entries in
      ``request_changes`` verdicts; reference the rule that would
      have caught the gap if present.
    scope:
      files:
        - services/api/treadmill_api/starters.py
        - services/api/tests/test_starters.py
        - docs/runbooks/edit-a-role-prompt.md
    validation:
      - kind: deterministic
        description: |
          test_starters.py passes; the three updated prompts each
          reference the ADR (0004 or 0030) and the relevant skill or
          artifact (plan diagram, AGENT.md).
        script: |
          cd services/api && uv run pytest tests/test_starters.py -q \
            && grep -q "ADR-0004" treadmill_api/starters.py \
            && grep -q "AGENT.md" treadmill_api/starters.py \
            && grep -q "ADR-0030" docs/runbooks/edit-a-role-prompt.md

  - id: rule-adr-and-plan-has-diagram
    title: Deterministic rule — ADRs and plans embed mermaid
    workflow: wf-author
    intent: |
      Author ``docs/knowledge-base/rules/adr-and-plan-has-diagram.yaml``
      + ``tools/rule-checks/adr-and-plan-has-diagram/check.sh``.

      check.sh: for every changed file under ``docs/adrs/`` or
      ``docs/plans/``, verify it contains a ```` ```mermaid ```` 
      block. Skip the false-positive case where the file itself only
      mentions the string ``mermaid`` in prose (the closing fence
      must be on its own line at column 0 — same regex the worker
      parser uses).

      Severity blocking.
    scope:
      files:
        - docs/knowledge-base/rules/adr-and-plan-has-diagram.yaml
        - tools/rule-checks/adr-and-plan-has-diagram/check.sh
        - services/api/tests/test_rules_schema.py
    validation:
      - kind: deterministic
        description: |
          Rule YAML parses; check.sh is executable; the schema test
          recognizes the new rule.
        script: |
          cd services/api && uv run pytest tests/test_rules_schema.py -q \
            && test -x tools/rule-checks/adr-and-plan-has-diagram/check.sh

  - id: rule-implementation-conforms-to-diagram
    title: LLM-judge rule — implementation conforms to diagram (fulfils ADR-0004 follow-up)
    workflow: wf-author
    intent: |
      Author
      ``docs/knowledge-base/rules/implementation-conforms-to-diagram.yaml``.
      No script — kind: llm-judge.

      The prompt instructs the judge to read the cited ADR/plan's
      mermaid diagram, the PR diff, apply ADR-0004's six conformance
      criteria, and return one of
      ``pass`` / ``fail-implementation`` / ``fail-diagram`` /
      ``uncertain``. The remediations encode ADR-0030 §3 severity:
      ``fail-implementation`` blocks merge; ``fail-diagram`` is
      advisory (prompts amendment); ``uncertain`` is advisory.
    scope:
      files:
        - docs/knowledge-base/rules/implementation-conforms-to-diagram.yaml
        - services/api/tests/test_rules_schema.py
    validation:
      - kind: deterministic
        description: |
          Rule YAML parses; the prompt names the four outcomes and
          references ADR-0004's checklist.
        script: |
          cd services/api && uv run pytest tests/test_rules_schema.py -q \
            && uv run --project services/api python -c "
          import yaml
          d = yaml.safe_load(open('../../docs/knowledge-base/rules/implementation-conforms-to-diagram.yaml'))
          checks = d.get('checks', [])
          assert any(c.get('type') == 'llm-judge' for c in checks), 'no llm-judge check'
          prompt = next(c['prompt'] for c in checks if c.get('type') == 'llm-judge')
          for token in ['pass', 'fail-implementation', 'fail-diagram', 'uncertain', 'ADR-0004']:
              assert token in prompt, f'prompt missing {token}'
          "

  - id: backfill-followup-plan
    title: Author the recursive backfill plan
    workflow: wf-author
    depends_on:
      - task.rule-adr-and-plan-has-diagram.pr_merged
      - task.rule-implementation-conforms-to-diagram.pr_merged
    intent: |
      Audit ``docs/adrs/*.md`` and ``docs/plans/*.md`` for files
      without a ``\`\`\`mermaid`` block. Author a follow-up plan at
      ``docs/plans/2026-05-14-adr-0030-diagram-backfill.md``
      (status: drafting) whose sequence_of_work fans out one
      wf-author task per missing-diagram artifact.

      Each fan-out task:
        - id: ``backfill-<basename-without-extension>``
        - workflow: wf-author
        - intent: read the ADR/plan, author a conformant mermaid
          diagram per ADR-0004 + ADR-0030, reflecting current
          implementation reality (per ADR-0030 §4); if reality is
          sub-optimal (Class C), open a learning at
          ``docs/learnings/2026-05-14-backfill-<slug>-gap.md``
          alongside the diagram
        - validation: deterministic — the touched ADR/plan now
          contains a ```` ```mermaid ```` block; implementation-
          conforms-to-diagram judge returns ``pass`` or
          ``fail-diagram`` (NOT ``fail-implementation``)

      Dispatching the follow-up plan is operator-mediated (out of
      scope of THIS plan).
    scope:
      files:
        - docs/plans/2026-05-14-adr-0030-diagram-backfill.md
    validation:
      - kind: deterministic
        description: |
          Follow-up plan exists; its sequence_of_work lists ≥ 33
          tasks (18 ADRs + 15 plans missing diagrams as of audit
          2026-05-14); each task has a validation script.
        script: |
          test -f docs/plans/2026-05-14-adr-0030-diagram-backfill.md \
            && uv run --project services/api python -c "
          import sys
          sys.path.insert(0, 'services/api')
          from treadmill_api.parsers.plan_doc import parse_plan_doc
          tasks = parse_plan_doc(open('docs/plans/2026-05-14-adr-0030-diagram-backfill.md').read())
          assert len(tasks) >= 33, f'only {len(tasks)} backfill tasks; want >= 33'
          for t in tasks:
              assert t.validation and t.validation[0].script, f'{t.id} missing validation script'
          "
```

## Decisions captured during execution

(empty)

## Post-mortem

Filled in on transition to `completed`/`abandoned`.
