---
status: active
trigger: ADR-0030 drafted 2026-05-14; operator reviewed + approved plan 2026-05-14; flipped active to dispatch via CLI submission.
parent: docs/adrs/0030-federated-in-repo-agent-context.md
---

# Plan: Federated in-repo agent context (ADR-0030 execution)

Land the author-setup + enforcement infrastructure that makes
mermaid diagrams in ADRs/plans and AGENT.md files at component roots
a hard requirement, not a convention. Recursive backfill of the 18
ADRs + 15 plans missing diagrams is dispatched via a follow-up plan
authored as the last task here (per Q30.e: fully recursive).

## Goal

After this plan executes:

1. Authors know what to build against — `/decide` and `/plan` skills
   carry ADR-0004's conformance checklist + diagram-type-by-decision-
   class guidance; `role-doc-author` references the skills;
   `role-code-author` reads the plan's diagram + any cited ADR's
   diagram before implementing.
2. The 6 component roots in Treadmill carry conformant AGENT.md
   files.
3. Four rules in `docs/knowledge-base/rules/` enforce the policy at
   PR-merge time, evaluated by ADR-0029's validation runner.
4. A follow-up plan is authored on disk that fans out one
   wf-author task per missing-diagram artifact, ready to dispatch
   recursively via Treadmill.

## Success criteria

- `.claude/skills/decide/SKILL.md` + `.claude/skills/plan/SKILL.md`
  each contain the ADR-0004 six-criterion conformance checklist +
  diagram-type table (sequence / flowchart / state-machine by
  decision class). Verifiable via grep.
- `services/api/treadmill_api/starters.py` carries updated prompts
  for role-doc-author, role-code-author, role-reviewer; tests in
  `test_starters.py` assert the new prompt content.
- Six AGENT.md files exist at component roots; each contains the
  five required section headers (Purpose, Key surfaces, Recent
  changes, Pitfalls, Navigation). Verifiable by deterministic grep.
- Four new rule YAMLs in `docs/knowledge-base/rules/` parse against
  ADR-0006's schema and have the right shape (deterministic +
  llm-judge mix per ADR-0030 §3).
- Follow-up plan exists on disk listing ≥ 33 backfill tasks (one per
  missing-diagram artifact); each task carries a task-intent
  validation script.

## Constraints / scope

### In scope

- Skill file edits to `.claude/skills/decide/`,
  `.claude/skills/plan/`.
- Role prompt updates in `services/api/treadmill_api/starters.py`
  (bootstrap fixture per ADR-0028) + `treadmill role update`
  documented for live deployments.
- AGENT.md schema doc at `docs/agent-md-schema.md`; six initial
  AGENT.md files at component roots.
- Per-repo override config schema at
  `docs/knowledge-base/rules/agent-md-locations.yaml`.
- Four new rule YAMLs + supporting `tools/rule-checks/<slug>/`
  scripts for the deterministic checks.
- One follow-up plan authored on disk that lists the backfill
  tasks; **dispatching** that plan is out of scope here.

### Out of scope

- Authoring the missing diagrams themselves (that work is the
  follow-up plan).
- A new workflow (`wf-doc-amend` or similar) — first cut uses
  existing `wf-author` against doc-only diffs.
- AGENT.md beyond the 6 Treadmill roots — other repos define their
  own locations once they enter the system (#95).
- Auto-generated structure diagrams (deferred per ADR-0030's
  rejected alternatives).

### Budget

One operator session for plan review + dispatch + sweep. The 9
tasks below are tractable for Treadmill to execute end-to-end given
ADR-0023 + ADR-0029 are now live.

## Diagram

See ADR-0030 §Diagram for the four-actor enforcement loop and the
AGENT.md topology. Not duplicated here.

## Risks / unknowns

- **role-code-author prompt growth.** Adding "read the plan's
  diagram + the cited ADR's diagram" inflates the prompt. Mitigation:
  keep the instruction terse; reference `.claude/skills/plan/`.
  Abort trigger: if prompt exceeds 4KB after edits, refactor into a
  skill the prompt references.
- **llm-judge cost.** Two new rules are llm-judge; both run on every
  PR touching docs or component surfaces. Mitigation: rules use
  haiku default per ADR-0029 Q29.b; severity for ambiguous verdicts
  is advisory, not blocking.
- **Backfill discovers Class C gaps.** Sub-optimality may surface
  faster than we can triage. Mitigation: backfill PRs are operator-
  reviewed; gaps land as learnings + ADR amendments, not silent
  fixes (per ADR-0030 §4).

## Sequence of work

```yaml
sequence_of_work:
  - id: skill-updates
    title: /decide and /plan skills carry ADR-0004 checklist + diagram-type guidance
    workflow: wf-author
    intent: |
      Update ``.claude/skills/decide/SKILL.md`` and
      ``.claude/skills/plan/SKILL.md`` to inline ADR-0004's six
      conformance criteria (named actors; labeled interactions;
      intent layer; right mermaid kind; sync vs async distinction;
      named alt branches) and the diagram-type table:

        * sequenceDiagram — actor-to-actor over time
        * flowchart — static topology / dependencies / layered
          architecture
        * stateDiagram-v2 — lifecycles and observable state
          transitions

      The skills are operator-facing AND read by role-doc-author
      indirectly (its prompt points at the skill files).
    scope:
      files:
        - .claude/skills/decide/SKILL.md
        - .claude/skills/plan/SKILL.md
    validation:
      - kind: deterministic
        description: |
          Both skill files contain the six conformance criteria
          (named actors / labeled interactions / intent layer /
          right kind / sync vs async / named alt) and the three
          diagram-type rows.
        script: |
          for f in .claude/skills/decide/SKILL.md .claude/skills/plan/SKILL.md; do
            grep -q "named actors" "$f" || { echo "missing named actors in $f"; exit 1; }
            grep -q "intent layer" "$f" || { echo "missing intent layer in $f"; exit 1; }
            grep -q "sequenceDiagram" "$f" || { echo "missing sequenceDiagram in $f"; exit 1; }
            grep -q "stateDiagram-v2" "$f" || { echo "missing stateDiagram-v2 in $f"; exit 1; }
            grep -qE "flowchart" "$f" || { echo "missing flowchart in $f"; exit 1; }
          done

  - id: role-prompt-updates
    title: role-doc-author + role-code-author + role-reviewer prompts wire ADR-0030 discipline
    workflow: wf-author
    depends_on:
      - task.skill-updates.pr_merged
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

  - id: agent-md-schema-doc
    title: AGENT.md schema document
    workflow: wf-author
    intent: |
      Author ``docs/agent-md-schema.md`` describing the five
      required sections (Purpose / Key surfaces / Recent changes /
      Pitfalls / Navigation) and the free-form-prose-within-section
      convention. Include a minimal example.

      Also author
      ``docs/knowledge-base/rules/agent-md-locations.yaml`` — a
      per-repo rule whose ``payload`` lists the directories where
      AGENT.md files belong. For Treadmill, the initial list is the
      6 component roots.
    scope:
      files:
        - docs/agent-md-schema.md
        - docs/knowledge-base/rules/agent-md-locations.yaml
    validation:
      - kind: deterministic
        description: |
          Schema doc exists and names all five sections; locations
          rule YAML parses and lists exactly the 6 Treadmill roots.
        script: |
          test -f docs/agent-md-schema.md \
            && for s in Purpose "Key surfaces" "Recent changes" Pitfalls Navigation; do
                 grep -q "$s" docs/agent-md-schema.md || { echo "missing $s"; exit 1; }
               done \
            && test -f docs/knowledge-base/rules/agent-md-locations.yaml \
            && uv run --project services/api python -c "
          import yaml, sys
          d = yaml.safe_load(open('docs/knowledge-base/rules/agent-md-locations.yaml'))
          locs = d.get('payload', {}).get('locations', [])
          expected = {'services/api', 'workers/agent', 'infra', 'tools/local-adapter', 'tools/dev-hooks', 'docs'}
          assert set(locs) == expected, f'got {set(locs)}, want {expected}'
          "

  - id: agent-md-seed
    title: Seed AGENT.md at the 6 component roots
    workflow: wf-author
    depends_on:
      - task.agent-md-schema-doc.pr_merged
    intent: |
      Author six AGENT.md files at the locations the schema doc
      enumerates: services/api, workers/agent, infra,
      tools/local-adapter, tools/dev-hooks, docs.

      Each file follows ``docs/agent-md-schema.md`` — five required
      section headers, free-form prose within. Content for each
      component should reflect current reality (per ADR-0030 §4's
      honest-current-state principle); if reality is sub-optimal,
      capture the gap as a learning at
      ``docs/learnings/<date>-agent-md-seed-<component>.md`` rather
      than papering over.
    scope:
      files:
        - services/api/AGENT.md
        - workers/agent/AGENT.md
        - infra/AGENT.md
        - tools/local-adapter/AGENT.md
        - tools/dev-hooks/AGENT.md
        - docs/AGENT.md
    validation:
      - kind: deterministic
        description: |
          All six AGENT.md files exist; each contains the five
          required section headers.
        script: |
          for d in services/api workers/agent infra tools/local-adapter tools/dev-hooks docs; do
            test -f "$d/AGENT.md" || { echo "missing $d/AGENT.md"; exit 1; }
            for s in "## Purpose" "## Key surfaces" "## Recent changes" "## Pitfalls" "## Navigation"; do
              grep -q "$s" "$d/AGENT.md" || { echo "$d/AGENT.md missing $s"; exit 1; }
            done
          done

  - id: rule-adr-and-plan-has-diagram
    title: Deterministic rule — ADRs and plans embed mermaid
    workflow: wf-author
    depends_on:
      - task.skill-updates.pr_merged
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
    depends_on:
      - task.skill-updates.pr_merged
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

  - id: rule-agent-md-section-presence
    title: Deterministic rule — AGENT.md files have all five sections
    workflow: wf-author
    depends_on:
      - task.agent-md-seed.pr_merged
    intent: |
      Author
      ``docs/knowledge-base/rules/agent-md-section-presence.yaml`` +
      ``tools/rule-checks/agent-md-section-presence/check.sh``.

      check.sh: for every changed AGENT.md file, verify the five
      required section headers are present (``## Purpose``,
      ``## Key surfaces``, ``## Recent changes``, ``## Pitfalls``,
      ``## Navigation``).

      Severity blocking.
    scope:
      files:
        - docs/knowledge-base/rules/agent-md-section-presence.yaml
        - tools/rule-checks/agent-md-section-presence/check.sh
        - services/api/tests/test_rules_schema.py
    validation:
      - kind: deterministic
        description: |
          Rule YAML parses; check.sh is executable; the schema test
          recognizes the new rule; running check.sh against the
          seeded files succeeds.
        script: |
          cd services/api && uv run pytest tests/test_rules_schema.py -q \
            && test -x tools/rule-checks/agent-md-section-presence/check.sh \
            && tools/rule-checks/agent-md-section-presence/check.sh services/api/AGENT.md

  - id: rule-docs-current-with-pr
    title: LLM-judge rule — docs current with PR (drift prevention)
    workflow: wf-author
    depends_on:
      - task.agent-md-seed.pr_merged
    intent: |
      Author
      ``docs/knowledge-base/rules/docs-current-with-pr.yaml``. No
      script — kind: llm-judge.

      The prompt instructs the judge to read the touched component's
      AGENT.md, any ADRs/plans cited in the PR description or
      changed files, and adjacent docs. Decide: did this PR change a
      component's externally-visible surface in a way that warranted
      doc updates? If yes, were they made? Both conditions must
      hold for pass.

      Severity blocking (this is the drift-prevention surface per
      ADR-0030 §3).
    scope:
      files:
        - docs/knowledge-base/rules/docs-current-with-pr.yaml
        - services/api/tests/test_rules_schema.py
    validation:
      - kind: deterministic
        description: |
          Rule YAML parses; the prompt references reading local
          relevant docs + surface-change detection.
        script: |
          cd services/api && uv run pytest tests/test_rules_schema.py -q \
            && uv run --project services/api python -c "
          import yaml
          d = yaml.safe_load(open('../../docs/knowledge-base/rules/docs-current-with-pr.yaml'))
          checks = d.get('checks', [])
          assert any(c.get('type') == 'llm-judge' for c in checks), 'no llm-judge check'
          prompt = next(c['prompt'] for c in checks if c.get('type') == 'llm-judge')
          for token in ['AGENT.md', 'surface', 'doc']:
              assert token.lower() in prompt.lower(), f'prompt missing {token}'
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

(empty — appended during execution as new decisions emerge)

## Post-mortem

Filled in when status transitions to `completed` or `abandoned`.
