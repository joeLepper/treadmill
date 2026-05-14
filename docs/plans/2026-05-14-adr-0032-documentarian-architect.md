---
status: superseded by 2026-05-14-hands-free-driving
trigger: ADR-0032 accepted 2026-05-14; superseded same-day by the consolidated hands-free-driving plan that bundles ADR-0031, ADR-0032, ADR-0033, and the four prereq tasks into a single dispatch so Treadmill can express cross-ADR dependencies natively.
parent: docs/adrs/0032-documentarian-and-architect-roles.md
---

# Plan: Documentarian + architect roles (ADR-0032 execution)

Ship the two roles, two workflows, output_kind taxonomy extension, verdict schema, and dispatch wiring ADR-0032 specifies, then re-fire the 33-task ADR-0030 backfill through `wf-doc-amend`.

## Goal

After this plan executes, Treadmill has `role-documentarian` + `role-architect` configured; `wf-doc-amend` and `wf-architecture-resolve` are defined workflows; the `documentation` output_kind routes correctly; the `ArchitectVerdict` Pydantic envelope validates judge output; `docs-current-with-pr` rule failures auto-dispatch `wf-doc-amend` as remediation; and the ADR-0030 backfill plan executes against the right roles.

## Success criteria

- `services/api/treadmill_api/output_kind.py` (or ADR-0022's equivalent) lists `documentation` and routes it to a disposition.
- `services/api/treadmill_api/events/architect_verdict.py` (or similar) defines `ArchitectVerdict` Pydantic model with `verdict` / `reasoning` / `target_artifact` / `remediation_summary` fields, matching ADR-0032's §Decision.
- `starters.py` carries `role-documentarian` (output_kind=documentation), `role-architect` (output_kind=analysis), and the two workflows. `test_starters.py` asserts the new entries.
- `workers/agent/treadmill_agent/runner_dispositions/documentation.py` exists; `wf-doc-amend` runs end-to-end against a real PR.
- `wf-architecture-resolve` dispatches the right downstream workflow per each of the four verdicts (amend / supersede / accept-as-is / uncertain).
- `coordination/triggers.py` dispatches `wf-doc-amend` on `docs-current-with-pr` rule failure (the 4th dispatch source documented in ADR-0029 lineage).
- `docs/plans/2026-05-14-adr-0030-diagram-backfill.md` re-authored with `workflow: wf-doc-amend` on all 33 tasks; CLI-submitted; tasks dispatch via documentarian rather than wf-author.

## Constraints / scope

### In scope

- All seven tasks below.
- DB-authoritative seeding for the two new roles via the existing `seed-starters` path (ADR-0028).
- Integration tests for the disposition flows + the trigger extension.

### Out of scope

- Periodic dispatch primitive (Q32.f deferred — needs a scheduling ADR).
- Architect-authored superseding ADRs (verdict=supersede dispatches wf-doc-amend; the actual superseding ADR is authored manually for v1 — see Q on this in §Risks).
- Auto-merge (ADR-0031 has its own plan).
- Documentation for the new roles' operator surface (lands as part of the ADR-0030 backfill — recursive).

### Budget

One operator session for plan review + dispatch + sweep. The seven tasks are tractable for Treadmill to execute end-to-end given ADR-0029's validation runner and ADR-0030's federated context are live.

## Diagram

See `docs/adrs/0032-documentarian-and-architect-roles.md` §Diagram for the descriptive/prescriptive workflow handoff.

## Risks / unknowns

- **Documentarian hesitation on `.claude/` edits** (task #123 pattern). Mitigation: role-documentarian's system_prompt explicitly authorizes edits to any path listed in the task's `scope.files`, including `.claude/skills/` and `docs/` paths. Abort trigger: if documentarian hesitates on ≥2 of the first 5 backfill tasks, escalate to a follow-up ADR on agent self-modification authorization.
- **Verdict-routing for `supersede`** is the riskiest verdict — it implies authoring a new ADR. v1 dispatches `wf-doc-amend` against `docs/adrs/<next-number>-*.md` and lets the documentarian author it; operator reviews before merge. If `wf-doc-amend` cannot author a from-scratch ADR (vs. amending existing), we abort + author the superseding ADR manually for that case.
- **`docs-current-with-pr` rule false-positives** spawn unnecessary `wf-doc-amend` runs. Mitigation: cap remediation dispatches per task at 5 (matches ADR-0029 Q29.e / ADR-0032 Q32.e).

## Sequence of work

```yaml
sequence_of_work:
  - id: output-kind-taxonomy-add-documentation
    title: Add documentation to OutputKind taxonomy + routing
    workflow: wf-author
    intent: |
      Per ADR-0032 Q32.a, add ``documentation`` as a new value in
      ADR-0022's ``OutputKind`` enum (likely
      ``services/api/treadmill_api/output_kind.py`` or wherever the
      enum lives — find it via grep). Update the routing layer
      that maps output_kind → disposition handler so
      ``documentation`` is recognized.

      ``documentation`` is distinct from ``plan_doc``: the latter's
      merge fires ADR-0021's plan-creation trigger; the former's
      merge does not — it amends an existing artifact in place.

      Tests in ``services/api/tests/test_output_kind.py``: enum
      includes ``documentation``; routing returns the
      documentation handler.
    scope:
      files:
        - services/api/treadmill_api/output_kind.py
        - services/api/tests/test_output_kind.py
        - workers/agent/treadmill_agent/runner.py
    validation:
      - kind: deterministic
        description: |
          OutputKind enum contains documentation; routing test
          recognizes it.
        script: |
          cd services/api && uv run pytest tests/test_output_kind.py -q \
            && grep -q "documentation" treadmill_api/output_kind.py

  - id: architect-verdict-schema
    title: ArchitectVerdict Pydantic envelope
    workflow: wf-author
    intent: |
      Per ADR-0032 Q32.d, author the Pydantic model
      ``ArchitectVerdict`` mirroring ADR-0027's ``ReviewVerdict``.

      Fields:
        verdict: Literal["amend", "supersede", "accept-as-is", "uncertain"]
        reasoning: str
        target_artifact: str
        remediation_summary: str | None = None

      Lives at
      ``services/api/treadmill_api/events/architect_verdict.py``
      (or similar event-schema location — match the ReviewVerdict
      module's home). Re-export from
      ``treadmill_api/events/__init__.py``.

      Tests: validate well-formed JSON; reject missing required
      fields; reject invalid verdict literal.
    scope:
      files:
        - services/api/treadmill_api/events/architect_verdict.py
        - services/api/treadmill_api/events/__init__.py
        - services/api/tests/test_architect_verdict.py
    validation:
      - kind: deterministic
        description: |
          Schema module exists; tests pass.
        script: |
          cd services/api && uv run pytest tests/test_architect_verdict.py -q \
            && uv run python -c "from treadmill_api.events import ArchitectVerdict; \
                  v = ArchitectVerdict(verdict='amend', reasoning='x', target_artifact='docs/adrs/0001-x.md'); \
                  print(v.model_dump_json())"

  - id: role-prompts-and-workflows
    title: role-documentarian + role-architect + wf-doc-amend + wf-architecture-resolve in starters.py
    workflow: wf-author
    depends_on:
      - task.output-kind-taxonomy-add-documentation.pr_merged
      - task.architect-verdict-schema.pr_merged
    intent: |
      Author both new roles + both new workflows in
      ``services/api/treadmill_api/starters.py``. Live deployments
      pick them up via ``treadmill role update`` /
      ``treadmill workflows seed-starters`` per ADR-0028; document
      the operator step in
      ``docs/runbooks/edit-a-role-prompt.md``.

      role-documentarian:
        - output_kind: documentation
        - model: haiku (per ADR-0029 Q29.b default; rules override)
        - system_prompt: reads task scope, reads cited code,
          authors doc amendment reflecting current reality per
          ADR-0030 §4. Explicitly authorizes edits to
          ``.claude/skills/`` and ``docs/`` paths. Opens learning
          + dispatches wf-architecture-resolve on Class C.

      role-architect:
        - output_kind: analysis
        - model: haiku
        - system_prompt: reads learning + implicated component,
          returns ``ArchitectVerdict`` JSON envelope. Biases toward
          accept-as-is for minor drift; high bar for supersede.

      wf-doc-amend: 1 step pointing at role-documentarian.

      wf-architecture-resolve: 1 step pointing at role-architect.
      Verdict routing happens in the disposition (not a separate
      step) — same pattern as wf-validate per ADR-0029.

      Update test_starters.py with the new entries.
    scope:
      files:
        - services/api/treadmill_api/starters.py
        - services/api/tests/test_starters.py
        - docs/runbooks/edit-a-role-prompt.md
    validation:
      - kind: deterministic
        description: |
          starters.py + test_starters.py have the new roles +
          workflows; runbook references ADR-0032.
        script: |
          cd services/api && uv run pytest tests/test_starters.py -q \
            && grep -q "role-documentarian" treadmill_api/starters.py \
            && grep -q "role-architect" treadmill_api/starters.py \
            && grep -q "wf-doc-amend" treadmill_api/starters.py \
            && grep -q "wf-architecture-resolve" treadmill_api/starters.py \
            && grep -q "ADR-0032" docs/runbooks/edit-a-role-prompt.md

  - id: wf-doc-amend-disposition
    title: workers/runner_dispositions/documentation.py handles wf-doc-amend
    workflow: wf-author
    depends_on:
      - task.role-prompts-and-workflows.pr_merged
    intent: |
      Author
      ``workers/agent/treadmill_agent/runner_dispositions/documentation.py``.
      Routes from runner.py on output_kind == 'documentation'.

      Disposition responsibilities (per ADR-0032 §wf-doc-amend):
        1. Read step output (the amended doc artifact).
        2. ``git add`` the artifact + ``git push`` to the task
           branch + ``gh pr create`` (or update existing per task
           #120 discipline once that lands).
        3. If the agent's output indicates a Class C gap surfaced
           (the documentarian's prompt instructs it to flag this
           in step output's metadata), open a learning file at
           ``docs/learnings/<date>-<slug>-gap.md`` and dispatch
           ``wf-architecture-resolve`` against the same task.

      Tests in
      ``workers/agent/tests/test_runner_dispositions.py``:
        - documentation disposition runs against a mock
          documentarian output; commits + pushes.
        - Class A/B path: no architecture-resolve dispatch.
        - Class C path: learning file written + dispatch fires.
    scope:
      files:
        - workers/agent/treadmill_agent/runner_dispositions/documentation.py
        - workers/agent/treadmill_agent/runner.py
        - workers/agent/tests/test_runner_dispositions.py
    validation:
      - kind: deterministic
        description: |
          documentation.py module exists; routing wired;
          dispositions tests pass.
        script: |
          cd workers/agent && uv run pytest tests/test_runner_dispositions.py -q \
            && grep -q "documentation" treadmill_agent/runner.py \
            && test -f treadmill_agent/runner_dispositions/documentation.py

  - id: wf-architecture-resolve-disposition
    title: workers/runner_dispositions extends to handle wf-architecture-resolve verdicts
    workflow: wf-author
    depends_on:
      - task.role-prompts-and-workflows.pr_merged
      - task.architect-verdict-schema.pr_merged
    intent: |
      Extend the analysis disposition (or author a new
      ``architecture.py`` disposition module) to handle the
      architect's step output:
        1. Parse step output into ``ArchitectVerdict`` Pydantic
           envelope. Reject malformed output (same discipline as
           ReviewVerdict per ADR-0027 §Decision).
        2. Route per verdict:
           - amend → dispatch wf-plan (or wf-author against an
             inline remediation task) to author the remediation
           - supersede → dispatch wf-doc-amend against
             ``docs/adrs/<next-number>-*.md`` (the documentarian
             authors the superseding ADR; operator reviews)
           - accept-as-is → dispatch wf-doc-amend against the
             target component's ``AGENT.md`` to append the gap to
             its Pitfalls section
           - uncertain → re-dispatch wf-architecture-resolve (cap
             at 5 attempts per task, mirroring ADR-0029 Q29.e)
        3. Cap enforcement: count prior
           ``wf-architecture-resolve`` runs for the task; if
           ≥ 5, log ``task.architecture_capped`` and skip.

      Tests in
      ``workers/agent/tests/test_runner_dispositions.py``:
        - parametrized over the 4 verdicts; each dispatches the
          right downstream workflow.
        - 5th uncertain skips with the capped log.
    scope:
      files:
        - workers/agent/treadmill_agent/runner_dispositions/architecture.py
        - workers/agent/treadmill_agent/runner.py
        - workers/agent/tests/test_runner_dispositions.py
    validation:
      - kind: deterministic
        description: |
          architecture disposition + tests pass.
        script: |
          cd workers/agent && uv run pytest tests/test_runner_dispositions.py -q \
            && test -f treadmill_agent/runner_dispositions/architecture.py

  - id: validator-remediation-dispatch
    title: docs-current-with-pr.fail → wf-doc-amend (4th dispatch source)
    workflow: wf-author
    depends_on:
      - task.wf-doc-amend-disposition.pr_merged
    intent: |
      Extend
      ``services/api/treadmill_api/coordination/triggers.py`` with
      a fourth dispatch source mirroring ADR-0029's
      ``maybe_dispatch_feedback_on_terminal_failure`` pattern:

        - When ``wf-validate.step.completed`` arrives with
          decision='fail' AND the failing check is
          ``docs-current-with-pr``, dispatch ``wf-doc-amend``
          against the affected docs (not ``wf-feedback``).
        - Extend ``dispatch_dedup._build_*_key`` for the new
          namespace (``docs-amend-run=<run_id>``).
        - Add a per-task cap on ``wf-doc-amend`` remediation
          dispatches (5 attempts per task across this source).

      Tests in
      ``services/api/tests/test_consumer_unit.py``:
        - docs-current-with-pr failure dispatches wf-doc-amend
          with docs-amend-run= namespace.
        - Same failure on a different rule (e.g.,
          adr-and-plan-has-diagram) dispatches wf-feedback (the
          existing path), NOT wf-doc-amend.
        - 5th wf-doc-amend dispatch skips with task.capped log.
    scope:
      files:
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/treadmill_api/coordination/dispatch_dedup.py
        - services/api/treadmill_api/coordination/consumer.py
        - services/api/tests/test_consumer_unit.py
        - services/api/tests/test_dispatch_dedup.py
    validation:
      - kind: deterministic
        description: |
          test_consumer_unit.py + test_dispatch_dedup.py pass;
          docs-amend-run namespace recognized; cap enforced.
        script: |
          cd services/api && uv run pytest tests/test_consumer_unit.py tests/test_dispatch_dedup.py -q

  - id: rebackfill-via-doc-amend
    title: Re-fire ADR-0030 backfill through wf-doc-amend
    workflow: wf-author
    depends_on:
      - task.wf-doc-amend-disposition.pr_merged
      - task.wf-architecture-resolve-disposition.pr_merged
    intent: |
      Re-author
      ``docs/plans/2026-05-14-adr-0030-diagram-backfill.md`` so
      every task in its sequence_of_work uses
      ``workflow: wf-doc-amend`` instead of ``workflow: wf-author``.

      Bump the plan's frontmatter ``status`` to mark a re-fire
      (e.g., add a ``trigger:`` note referencing this plan).

      No code changes — only the plan doc is edited. The CLI
      re-submission of the plan dispatches the 33 tasks through
      the new workflow.

      Tests: re-parse the plan; assert all 33 tasks now reference
      wf-doc-amend; assert validation scripts still match.
    scope:
      files:
        - docs/plans/2026-05-14-adr-0030-diagram-backfill.md
    validation:
      - kind: deterministic
        description: |
          Backfill plan now routes all 33 tasks through
          wf-doc-amend.
        script: |
          uv run --project services/api python -c "
          import sys
          sys.path.insert(0, 'services/api')
          from treadmill_api.parsers.plan_doc import parse_plan_doc
          tasks = parse_plan_doc(open('docs/plans/2026-05-14-adr-0030-diagram-backfill.md').read())
          assert len(tasks) == 33, f'expected 33 tasks, got {len(tasks)}'
          for t in tasks:
              assert t.workflow == 'wf-doc-amend', f'{t.id} still uses {t.workflow}'
          "
```

## Decisions captured during execution

(empty)

## Post-mortem

Filled in on transition to `completed`/`abandoned`.
