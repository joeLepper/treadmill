---
auto_merge: false
status: active
---

# Plan: Inject documentary context into the llm-judge runner (ADR-0052 validator wave 1)

- **Status:** active
- **Date:** 2026-05-21
- **Related ADRs:** ADR-0052 (judgment-role tuning; triage), ADR-0030 (docs-current-with-pr), ADR-0029 (validator/rule engine)

## Goal

Fix the root cause of the validator's docs-currency false-pass: `run_llm_judge`
composes the judge prompt from only the diff + task spec, but llm-judge rule
prompts (e.g. `docs-current-with-pr`'s `surface-changes-have-doc-updates`)
declare they receive `AGENT_MD` and other documentary inputs that are never
supplied. Starved of the component's `AGENT.md`, the judge concludes none exists
and false-passes. Inject the relevant `AGENT.md` context so the judge can see it.

**Manual-merge (`auto_merge: false`):** a live validation-path change, and a
second orchestrator session is active — the operator reviews and merges only
when clean + conflict-free.

## Success criteria

- `run_llm_judge` injects the relevant component `AGENT.md` content into the
  llm-judge prompt (under an `AGENT_MD` section) whenever the diff touches files
  under a directory tree that has one.
- A judge run on a diff that changes a surface in a component WITH an `AGENT.md`
  now *sees* that `AGENT.md` in its prompt (verified by a unit test on the
  composed prompt), closing the "no AGENT.md exists" false-pass.
- Repairs the whole llm-judge population (any context-dependent judge benefits),
  not just docs-currency.

## Constraints / scope

### In scope
The `run_llm_judge` context injection + a self-contained `AGENT.md`-gathering
helper, its tests, and the component doc update.

### Out of scope
Changing any rule YAML / judge prompts (`docs/knowledge-base/rules/`); cited
ADR/plan + adjacent-doc injection (a focused follow-up — this wave does
`AGENT.md`, the proven false-pass); the architect; `services/api`.

### Budget
One task. If the helper can't be unit-tested without a live Claude call, it
fails the deterministic check rather than merging.

## sequence_of_work

```yaml
sequence_of_work:
  - id: judge-context-injection
    title: Inject component AGENT.md context into run_llm_judge (ADR-0052)
    workflow: wf-author
    intent: |
      Fix the llm-judge input-starvation bug. ``run_llm_judge`` in
      ``workers/agent/treadmill_agent/validation_runtime.py`` composes the
      judge prompt from only ``check.prompt`` + the diff + task_spec, but
      llm-judge rule prompts (e.g. ``docs-current-with-pr``) state they receive
      an ``AGENT_MD`` input that is never supplied — so the judge can't see the
      component's AGENT.md and false-passes ("no AGENT.md exists"). Read the
      file first (the ``run_llm_judge`` function around line 177).

      (1) Add a module-level helper in the same file:

        def gather_agent_md_context(repo_dir: Path, diff: str) -> str:
            '''Return the content of every AGENT.md that governs a file touched
            by ``diff`` — the nearest AGENT.md walking up from each touched
            path to ``repo_dir``. Empty string if none. Repo-agnostic (no
            dependency on a rule file) so it works for any onboarded repo.'''

      Implement it to:
        - Parse touched paths from the unified ``diff``: collect the post-image
          paths from lines starting with ``+++ b/`` (strip the ``b/`` prefix);
          ignore ``/dev/null``. (These cover adds + modifies; deletes resolve to
          /dev/null and are skipped, which is fine.)
        - For each touched path, walk up its parent directories within
          ``repo_dir`` looking for an ``AGENT.md``; record the first one found
          (nearest ancestor). Collect the UNIQUE set of AGENT.md paths.
        - Read each (utf-8, errors="replace"); skip any that don't exist or
          can't be read. Return a block: for each, ``### <relpath-from-repo_dir>``
          on its own line, then the file content, separated by blank lines.
          Return ``""`` if the set is empty.

      (2) In ``run_llm_judge``, build the context via
      ``gather_agent_md_context(repo_dir, diff)`` and, when it is non-empty,
      include it in the composed ``prompt`` as a clearly-labelled section
      ``## AGENT_MD`` (place it before ``## PR diff``). Do not otherwise change
      the prompt, the diff/task_spec sections, or the claude invocation. Guard
      against a None/empty diff.

      (3) Tests — scope IN the existing ``workers/agent/tests/test_validation_runtime.py``
      (do not create a separate file; the existing suite covers run_llm_judge and
      must stay green). Add:
        - ``test_gather_agent_md_context_*``: create a tmp repo dir with
          ``services/api/AGENT.md`` (and content) + a nested file; a diff
          touching ``services/api/foo.py`` returns a block containing the
          AGENT.md content + the ``### services/api/AGENT.md`` header; a diff
          touching a path with no ancestor AGENT.md returns ``""``; dedupes when
          two touched files share one AGENT.md.
        - ``test_run_llm_judge_includes_agent_md``: patch
          ``treadmill_agent.claude_code.run_claude`` (match how existing tests
          patch it) to capture the ``prompt`` arg; call ``run_llm_judge`` with a
          repo_dir + a diff touching a component that has an AGENT.md; assert the
          composed prompt contains an ``AGENT_MD`` section with that file's
          content. Keep any existing run_llm_judge tests passing (update them if
          the prompt shape assertion changed).

      (4) DOCS (ADR-0030 docs-current-with-pr — REQUIRED): update
      ``workers/agent/AGENT.md`` — note that ``run_llm_judge`` now injects the
      touched components' AGENT.md into llm-judge prompts (Key surfaces / Recent
      changes), closing the docs-currency false-pass.
    scope:
      files:
        - workers/agent/treadmill_agent/validation_runtime.py
        - workers/agent/tests/test_validation_runtime.py
        - workers/agent/AGENT.md
      out_of_scope:
        - docs/knowledge-base/rules/docs-current-with-pr.yaml
        - services/api/treadmill_api/coordination/triggers.py
    validation:
      - kind: deterministic
        description: |
          The gather helper exists and the validation_runtime test suite passes.
        script: |
          cd workers/agent \
            && grep -q "def gather_agent_md_context" treadmill_agent/validation_runtime.py \
            && uv run pytest tests/test_validation_runtime.py -q
```

## Risks / unknowns

- **Large AGENT.md blocks** could bloat the judge prompt — acceptable; AGENT.md
  files are small. A token cap is a follow-up if needed.
- **Diff-path parsing** must handle adds/modifies/deletes; the deterministic
  test covers the common shapes.
- **Concurrent sessions:** merge only after confirming no conflict with the
  other orchestrator's main changes (it is on the ramjac bootstrap, different
  files).

## Decisions captured during execution

- **Fix the runner, not the prompts** — the judge prompts already declare the
  inputs; the bug is that the runner never supplied them.

## Post-mortem

_(filled when the wave completes)_
