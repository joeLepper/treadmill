---
auto_merge: true
status: active
---

# Plan: Add `make` (build-essential) to the agent image

- **Status:** active
- **Date:** 2026-05-27

## Goal

Unblock plans whose validation script uses `make <target>`. The current
`workers/agent/Dockerfile` installs `git`, `gh`, `nodejs`, Claude Code
CLI, and `uv` — but **NOT `make`**. Any plan with `make X` in its
`validation.script` fails the worker's author-side gate with `exit 127:
make: not found`, no commit lands, and the architect's downstream verdict
becomes "no commits on branch → amend" → wf-feedback → loop → cap →
terminal-give-up. A small number of downstream-repo tasks ran 11 iterations
last night through exactly this path before the wf-feedback 5-cap
escalated them.

Two-line Dockerfile change: add `build-essential` (or just `make`) to the
existing `apt-get install` block. The agent rebuild + autodeploy then
flows through the now-fixed chain (recreate + git-pull-before-build).

## Success criteria

- `docker run --rm --entrypoint sh treadmill-agent:dev -c 'make --version'`
  returns a real version string (not `make: not found`).
- A future plan whose `validation.script` is `make <target>` runs the
  target instead of failing exit 127.
- Existing agent build + tests stay green; the change is additive.

## Constraints / scope

### In scope
`workers/agent/Dockerfile` — add `make` to the existing `apt-get install`
list. Plus a Dockerfile-grep test to lock the dependency.

### Out of scope
- `cmake`, `g++`, full toolchain — start with `make`; add the others only
  if a future plan needs them.
- Per-repo build_profile detection of lowercase `makefile` (the discovery
  enhancement; separate plan track).
- Rewriting downstream-repo plans' validation scripts to use direct
  shell commands instead of `make` — that's operator-hands on the
  downstream-repo side; this plan ships the upstream unblock so they
  don't have to.

### Budget
One task, `auto_merge: true`. Trivial Dockerfile change.

## sequence_of_work

```yaml
sequence_of_work:
  - id: add-make-to-agent-image
    title: Add make to the agent Dockerfile (unblock make-based validation scripts)
    workflow: wf-author
    intent: |
      Edit ``workers/agent/Dockerfile``. Find the existing
      ``RUN apt-get update && apt-get install -y --no-install-recommends \``
      block (around line 11). Add ``make`` to the package list — alongside
      ``git``, ``ca-certificates``, ``curl``, ``gnupg`` (or wherever they
      sit in the list). Pick the minimal package: prefer just ``make``
      over ``build-essential`` to keep image size small (no g++/dpkg-dev
      needed at this point; future plans that need them can add later).

      Add NO other dependencies. Do NOT reorganize the Dockerfile, do NOT
      restructure the RUN blocks, do NOT add `cmake`/`gcc`/etc. Single
      additive change.

      Tests at the exact path
      ``tools/local-adapter/tests/test_agent_image_has_make.py`` (this
      lives in local-adapter tests because that's where the agent image
      is built + tested in the existing harness):
        * structural: ``open(workers/agent/Dockerfile).read()`` contains
          the word ``make`` on the same line as ``apt-get install``.
        * Keep existing agent-image tests green.

      DOCS (ADR-0030 — REQUIRED): update ``workers/agent/AGENT.md`` —
      add a Recent changes entry noting ``make`` is now installed in the
      agent image so plans can use ``make <target>`` in their validation
      scripts.
    scope:
      files:
        - workers/agent/Dockerfile
        - tools/local-adapter/tests/test_agent_image_has_make.py
        - workers/agent/AGENT.md
      out_of_scope:
        - workers/agent/treadmill_agent/
        - services/api/
    validation:
      - kind: deterministic
        description: |
          Dockerfile mentions ``make`` on an apt-get install line and the
          exact-path test passes.
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          grep -E "apt-get install.*make|make[ \\\\].*apt-get install" "$ROOT/workers/agent/Dockerfile" \
            && [ -f "$ROOT/tools/local-adapter/tests/test_agent_image_has_make.py" ] \
            && cd "$ROOT/tools/local-adapter" && uv run pytest tests/test_agent_image_has_make.py -q
```

## Risks / unknowns

- **Image size:** `make` alone is ~1 MB; `build-essential` would be ~250 MB.
  We pick `make` only.
- **Existing agent builds:** the apt-get install line already runs;
  adding one package keeps the layer cache layout unchanged.
- **Downstream-repo plans that use other build tools** (cmake, ninja,
  etc.): if those surface, a follow-on plan adds them. Don't speculate.

## Post-mortem

_(filled when the wave completes)_
