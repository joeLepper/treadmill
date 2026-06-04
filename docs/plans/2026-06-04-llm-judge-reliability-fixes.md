---
auto_merge: true
status: completed
---

# Plan: LLM-judge reliability fixes — input injection + output contract

- **Status:** active
- **Date:** 2026-06-04
- **Related ADRs:** ADR-0027 (structured JSON envelope), ADR-0047 (deterministic-where-possible), ADR-0052 (human-labeled corpora)
- **Related plan:** docs/plans/2026-05-21-judgment-role-prompt-tuning.md (Decisions captured — root-causes the issues this plan fixes)

## Goal

Two pure-code fixes to the `run_llm_judge` runtime that repair the
context-dependent llm-judge population system-wide. Both are surfaced by
the 2026-05-21 judgment-role plan's "Decisions captured" section as
**code bugs, not prompt-tuning targets** — fixing them is the
prerequisite for any DSPy work on these judges to be meaningful.

## Success criteria

- `run_llm_judge` injects `CITED_ADRS`, `CITED_PLANS`, and
  `ADJACENT_DOCS` blocks into the prompt alongside `AGENT_MD`, matching
  the inputs declared by rule prompts like
  `docs/knowledge-base/rules/docs-current-with-pr.yaml`. The `AGENT_MD`
  injection already shipped.
- `_parse_validation_envelope` accepts `result` as an alias for
  `verdict` in the JSON envelope, recovering the population of judge
  runs that errored on `result`-vs-`verdict` Pydantic mismatch.
- New tests pin each behavior at `workers/agent/tests/test_validation_runtime.py`.

## Constraints / scope

### In scope

- `workers/agent/treadmill_agent/validation_runtime.py` — extend the
  `run_llm_judge` prompt-assembly path with three new gather helpers
  (sibling pattern to `gather_agent_md_context`); add `result` alias
  to `ValidationVerdict` / parser.
- `workers/agent/tests/test_validation_runtime.py` — pin both behaviors.
- `workers/agent/AGENT.md` — Recent-changes entry per task.

### Out of scope

- The labeling UI / corpus tooling (separate plan; needs operator
  labeling pass).
- DSPy optimization of any specific judge prompt (Wave 4 covers
  retrospective scoring; corpus-driven optimization is downstream of
  the labeling UI substrate).
- Re-architecting the rule schema — these rules already declare the
  inputs in their prompt text; the runtime just isn't supplying them.
- `validation-script-executed` — ADR-0047 already demoted it to
  warning via deterministic sibling `pytest-collect-pass`.

### Budget

Two sequential tasks. Sequential to avoid the `workers/agent/AGENT.md`
"Recent changes" hotspot conflict; each task is one-file-plus-tests
work, mechanically small.

## sequence_of_work

```yaml
sequence_of_work:
  - id: llm-judge-extend-input-injection
    title: Inject CITED_ADRS + CITED_PLANS + ADJACENT_DOCS into run_llm_judge
    workflow: wf-author
    intent: |
      Extend ``run_llm_judge`` in
      ``workers/agent/treadmill_agent/validation_runtime.py`` to inject
      three additional context blocks into the prompt, parallel to the
      existing ``gather_agent_md_context`` / ``AGENT_MD`` injection
      (which already shipped — see line ~269).

      The motivation (from the 2026-05-21 judgment-role plan, "Decisions
      captured during execution"): rules like
      ``docs/knowledge-base/rules/docs-current-with-pr.yaml`` declare
      these inputs in their prompt body, but the runtime only injects
      the diff + task_spec (and now AGENT_MD). Judges are starved of the
      context they're asked to evaluate against, and conclude that
      "none exists" → false pass. Fixing the injection repairs the
      whole population, not just docs-currency.

      Implementation:

      1. Add three helpers parallel to ``gather_agent_md_context``:
         - ``gather_cited_adrs_context(diff: str, repo_dir: Path) -> str``
           — extract ``ADR-NNNN`` references from the diff text + each
           file's content in the diff; read each match's ADR file from
           ``docs/adrs/NNNN-*.md`` (glob); return concatenated blocks.
         - ``gather_cited_plans_context(diff: str, repo_dir: Path) -> str``
           — same shape, for plans referenced by path
           (``docs/plans/YYYY-MM-DD-...md``).
         - ``gather_adjacent_docs_context(diff: str, repo_dir: Path) -> str``
           — for each touched file, find docs in the same directory or
           its ``docs/`` sibling (e.g., README.md, *.md siblings,
           ``../docs/*.md`` adjacent); cap total size at ~50k chars to
           avoid prompt bloat.

      2. Wire them into the prompt body next to ``agent_md_section``:
         ```python
         cited_adrs = gather_cited_adrs_context(diff or "", repo_dir)
         cited_plans = gather_cited_plans_context(diff or "", repo_dir)
         adjacent_docs = gather_adjacent_docs_context(diff or "", repo_dir)
         cited_adrs_section = f"## CITED_ADRS\n{cited_adrs}\n\n" if cited_adrs else ""
         cited_plans_section = f"## CITED_PLANS\n{cited_plans}\n\n" if cited_plans else ""
         adjacent_docs_section = f"## ADJACENT_DOCS\n{adjacent_docs}\n\n" if adjacent_docs else ""
         ```
         Insert all three between the existing ``agent_md_section`` and
         ``## PR diff`` block. Order: AGENT_MD → CITED_ADRS →
         CITED_PLANS → ADJACENT_DOCS → diff → task_spec.

      3. Each helper degrades gracefully — IOError / decoding errors
         skip the file (sibling pattern to the AGENT.md gatherer);
         missing/empty result returns empty string so the corresponding
         section is omitted.

      Tests at ``workers/agent/tests/test_validation_runtime.py``
      (sibling pattern to existing tests in that file):

      - For each helper: pass a tmp ``repo_dir`` with a small fixture
        (one ADR, one plan, one adjacent doc), pass a diff that
        references each, assert the returned block contains the file
        content with a header.
      - For ``run_llm_judge``: with the prompt-assembly path, mock
        ``claude_code.run_claude`` to capture the composed prompt;
        assert all three section headers appear in order when content
        is present, and are absent when content is empty.
      - Adjacent-docs size cap: write a fixture that would exceed 50k
        chars; assert truncation.

      DOCS: add a Recent-changes bullet to
      ``workers/agent/AGENT.md`` naming the three new helpers + the
      population repair motivation.
    scope:
      files:
        - workers/agent/treadmill_agent/validation_runtime.py
        - workers/agent/tests/test_validation_runtime.py
        - workers/agent/AGENT.md
      out_of_scope:
        - services/api/
        - docs/knowledge-base/rules/
        - cli/
    validation:
      - kind: deterministic
        description: |
          All three helpers exist, are wired into run_llm_judge, and
          tests pass.
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          grep -q "def gather_cited_adrs_context" "$ROOT/workers/agent/treadmill_agent/validation_runtime.py" \
            && grep -q "def gather_cited_plans_context" "$ROOT/workers/agent/treadmill_agent/validation_runtime.py" \
            && grep -q "def gather_adjacent_docs_context" "$ROOT/workers/agent/treadmill_agent/validation_runtime.py" \
            && grep -q "CITED_ADRS" "$ROOT/workers/agent/treadmill_agent/validation_runtime.py" \
            && grep -q "CITED_PLANS" "$ROOT/workers/agent/treadmill_agent/validation_runtime.py" \
            && grep -q "ADJACENT_DOCS" "$ROOT/workers/agent/treadmill_agent/validation_runtime.py" \
            && cd "$ROOT/workers/agent" && uv run pytest tests/test_validation_runtime.py -q

  - id: llm-judge-output-contract-alias
    title: Accept `result` as alias for `verdict` in validation envelope
    workflow: wf-author
    depends_on: [task.llm-judge-extend-input-injection.pr_merged]
    intent: |
      Widen ``ValidationVerdict`` + ``_parse_validation_envelope`` in
      ``workers/agent/treadmill_agent/validation_runtime.py`` to accept
      ``{"result": "pass" | "fail", "rationale": "..."}`` as equivalent
      to ``{"verdict": ...}``.

      Motivation (from the 2026-05-21 judgment-role plan, validator
      corpus triage): **116 judge runs errored** because the model
      emitted ``result`` instead of ``verdict``, hitting a Pydantic
      ``ValidationError`` in the strict envelope. That's not a
      prompt-tuning problem — those judges' verdicts were lost to a
      brittle output contract. The 2026-05-21 plan explicitly classifies
      this as a code bug.

      Implementation: use Pydantic's field alias mechanism. The cleanest
      shape (Pydantic v2):

      ```python
      from pydantic import BaseModel, ConfigDict, Field

      class ValidationVerdict(BaseModel):
          model_config = ConfigDict(populate_by_name=True)
          verdict: Literal["pass", "fail"] = Field(alias="result")
          rationale: str
      ```

      Pydantic's v2 alias behavior: ``populate_by_name=True`` means the
      model accepts EITHER the alias (``result``) OR the field name
      (``verdict``); the model's attribute is always ``.verdict`` after
      parsing. ``_parse_validation_envelope`` keeps returning
      ``parsed.verdict`` and callers are untouched.

      Tests at ``workers/agent/tests/test_validation_runtime.py``:
      - ``{"verdict": "pass", "rationale": "x"}`` parses, ``verdict ==
        "pass"``.
      - ``{"result": "pass", "rationale": "x"}`` parses, ``verdict ==
        "pass"``.
      - ``{"result": "fail", "rationale": "x"}`` parses, ``verdict ==
        "fail"``.
      - Both keys present with conflicting values → behavior pin
        (Pydantic typically takes the field name when both are present
        with ``populate_by_name=True``; pin whichever it actually does).
      - Invalid verdict value (``"maybe"``) still rejects.
      - Missing both → still rejects (existing error path).

      DOCS: add a Recent-changes bullet to
      ``workers/agent/AGENT.md`` naming the alias addition + the
      "116 errored runs" motivation.
    scope:
      files:
        - workers/agent/treadmill_agent/validation_runtime.py
        - workers/agent/tests/test_validation_runtime.py
        - workers/agent/AGENT.md
      out_of_scope:
        - services/api/
        - workers/agent/treadmill_agent/judge_eval.py
        - cli/
    validation:
      - kind: deterministic
        description: |
          Alias is wired; both keys parse; tests pass.
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          grep -q "populate_by_name" "$ROOT/workers/agent/treadmill_agent/validation_runtime.py" \
            && grep -q "alias=\"result\"" "$ROOT/workers/agent/treadmill_agent/validation_runtime.py" \
            && cd "$ROOT/workers/agent" && uv run pytest tests/test_validation_runtime.py -q
```

## Risks / unknowns

- **ADJACENT_DOCS size bloat** — capped at ~50k chars; if real-world
  prompts still blow past Claude's context window, the cap may need
  tightening or a smarter "most relevant adjacent docs" selector.
  Acceptable risk for v1.
- **Pydantic alias double-key behavior** — covered by the conflicting-
  values test. Whatever Pydantic does, pin it explicitly so future
  upgrades don't silently change behavior.
- **AGENT.md Recent-changes hotspot** — sequential `depends_on` avoids
  the parallel-PR conflict pattern we keep hitting.

## Post-mortem

_(filled when the plan completes)_
