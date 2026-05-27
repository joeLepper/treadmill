---
auto_merge: true
status: active
---

# Plan: Add `aws-cdk` (CLI) to the agent image

- **Status:** active
- **Date:** 2026-05-27
- **Sibling:** PR #23 (added `make` to the agent image, same pattern).

## Goal

Unblock plans whose validation script uses `cdk <command>` (e.g.
`cdk synth`). The agent image installs `gh`, `nodejs`, `claude-code` (via
npm), `uv`, `git`, and now `make` — but not `aws-cdk`. Any plan that
calls `cdk` exits 127 (command not found) at the worker's author-side
validation gate, no commit lands, and the architect-amend → wf-feedback
→ cap path fires. One downstream-repo task hit exactly this pattern
this morning before its wf-feedback cap escalated it.

Single npm install added alongside the existing `claude-code` line.
Image growth ~50 MB.

## Success criteria

- `docker run --rm --entrypoint sh treadmill-agent:dev -c 'cdk --version'`
  returns a real version string (not `cdk: not found`).
- A future plan whose `validation.script` is `cdk synth <stack>` runs
  the synth instead of failing exit 127.
- Existing agent build + tests stay green.

## Constraints / scope

### In scope
`workers/agent/Dockerfile` — add `aws-cdk` to an existing or new
`npm install -g` line. Plus a structural test mirroring
`test_agent_image_has_make.py`.

### Out of scope
- AWS account credentials inside the worker (cdk synth in a worker
  context generally doesn't need account access; cdk deploy would, but
  no plan should be running cdk deploy from a worker).
- Pinning the cdk version (let npm resolve to the latest stable;
  reproducibility is the agent-image build's domain, not this fix).

### Budget
One task, `auto_merge: true`. Trivial Dockerfile change.

## sequence_of_work

```yaml
sequence_of_work:
  - id: add-cdk-to-agent-image
    title: Add aws-cdk to the agent Dockerfile (unblock cdk-based validation scripts)
    workflow: wf-author
    intent: |
      Edit ``workers/agent/Dockerfile``. The existing ``claude-code`` line
      (around line 29) is ``RUN npm install -g
      @anthropic-ai/claude-code@2.1.138``. Either:

        (a) APPEND ``aws-cdk`` to the same npm install line (single layer,
            slightly smaller image), or

        (b) ADD a new line ``RUN npm install -g aws-cdk`` right after the
            claude-code line.

      Either is acceptable; pick whichever keeps the diff smallest. Do
      NOT pin the cdk version (the goal is "make cdk work in workers" —
      reproducibility per-build is the image's job).

      NO other dependencies. Don't add ``cdk-typescript``, ``cdk-python``,
      ``aws-cli``, etc. Single additive change.

      Tests at the EXACT path
      ``tools/local-adapter/tests/test_agent_image_has_cdk.py`` —
      mirror ``test_agent_image_has_make.py``: read the Dockerfile, scan
      for ``aws-cdk`` in an npm install context. (Don't try to build the
      actual image in the test — that's slow and the existing image-build
      tests cover that.)

      DOCS (ADR-0030 — REQUIRED): update ``workers/agent/AGENT.md`` —
      Recent changes entry noting cdk is now in the agent image so
      plans can use ``cdk <command>`` in their validation scripts.
    scope:
      files:
        - workers/agent/Dockerfile
        - tools/local-adapter/tests/test_agent_image_has_cdk.py
        - workers/agent/AGENT.md
      out_of_scope:
        - workers/agent/treadmill_agent/
        - services/api/
    validation:
      - kind: deterministic
        description: |
          Dockerfile contains ``aws-cdk`` in an ``npm install`` context
          and the exact-path test passes.
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          grep -E "npm install.*aws-cdk|aws-cdk" "$ROOT/workers/agent/Dockerfile" \
            && [ -f "$ROOT/tools/local-adapter/tests/test_agent_image_has_cdk.py" ] \
            && cd "$ROOT/tools/local-adapter" && uv run pytest tests/test_agent_image_has_cdk.py -q
```

## Risks / unknowns

- **Image size:** ~50 MB for cdk; acceptable.
- **npm install ordering:** if combined with claude-code's line, the
  whole layer rebuilds when either pkg changes. Acceptable — these are
  rare changes.

## Post-mortem

_(filled when the wave completes)_
