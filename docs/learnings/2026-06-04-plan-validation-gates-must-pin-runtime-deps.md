---
title: Plan validation gates must pin their own runtime deps
date: 2026-06-04
status: open
tags: [plan-authoring, validation-gates, worker-sandbox, plan-validate, sandbox-safety]
related_adrs: [ADR-0058, ADR-0059]
related_plans:
  - docs/plans/2026-06-02-adr-0065-autoscaler-smoke-gate-implementation.md
---

## Observation

ADR-0065 Step 2 (`task aa9c7504`) was wedged for ~3 hours and
escalated by `wf-stuck-task-sweep-stalled`. The worker dispatched
6 wf-author runs over that window. Each one produced a
structurally-valid GitHub Actions workflow file. Each one was
rejected by the same deterministic gate:

```yaml
- kind: deterministic
  script: |
    uv run python -c "import yaml; yaml.safe_load(open('.github/workflows/autoscaler_smoke.yml'))"
```

Verdict `fail`, log excerpt:

```
ModuleNotFoundError: No module named 'yaml'
```

The worker sandbox image (`workers/agent/Dockerfile`) does not
install PyYAML. The gate looked sandbox-safe at plan-authoring time
because PyYAML is installed in every API and dashboard package the
plan author touches on the host; `import yaml` Just Works during
local plan-validate. It silently fails the moment the worker
executes the same one-liner without `pyproject.toml`-mediated deps.

After the 6th rejection the architect verdicted `amend` and
dispatched a wf-plan recovery — which also never spawned a
workflow_run (separate wedge pattern). The task escalated as
`wf-stuck-task-sweep-stalled` and was hand-shipped via PR #153.

## Why this matters

This is the **third** instance of "validator references a binary
or import that the sandbox doesn't carry" in <2 months:

1. ADR-0058 / ADR-0059 (2026-05-27): `import aws_cdk` in a
   validation script — Python lib absent even though the `cdk`
   binary was installed. Memo'd as
   `feedback_verify_binaries_exist_in_sandbox.md`.
2. PR #58 (same week): `pytest` against the full workspace —
   gates the worker can't reach because dev deps aren't installed
   in the agent image. Bounded by ADR-0059 per-repo `worker_deps`.
3. **PR #153** (2026-06-04, today): `import yaml` — PyYAML in
   pyproject but not in the agent image's pinned environment.

The pattern is: any plan validation gate that runs a Python
one-liner with a library import or a non-stdlib CLI is gambling
against the sandbox's actual installed surface. `treadmill plan
validate` checks structural shape only; it does NOT execute the
script. Plan authors keep losing the gamble.

## What we should do

Three options, increasing in invasiveness:

1. **Author-side discipline (current).** Memo'd as
   `feedback_verify_binaries_exist_in_sandbox.md`; relies on the
   author remembering. Today's hit proves memory alone is not
   enough.
2. **Plan-validate extension (recommended).** Make `treadmill
   plan validate` extract every `python -c "import X"` and every
   bare-CLI invocation from `validation_script` blocks, then
   verify each against the agent image's installed surface
   (parse `workers/agent/Dockerfile`'s `pip install` /
   `apt install` invocations + the worker_deps spec). Refuse the
   plan with an actionable diagnostic when a gate references
   something the sandbox can't carry.
3. **Validator-side `uv run --with <pkg>`.** Every Python
   one-liner in a plan validation gate gets prefixed with `uv run
   --with <pkg>` so the runtime fetches the dep on demand. Makes
   the gate self-sufficient at the cost of a per-run download. Not
   a substitute for (2) because grep-style gates still depend on
   the system PATH.

The leverage in (2) is high: one mechanical check at plan-submit
time would have caught all three instances above.

## Action items

- [ ] Extend `cli/treadmill_cli/plan_validate.py` to parse `python
      -c "import X"` patterns and verify each against the agent
      image's pinned environment. Surface mismatches as actionable
      errors, not warnings.
- [ ] Extend the same parse to flag bare-CLI invocations
      (`pytest`, `cdk`, `gh`, `jq`, etc.) and verify against
      `workers/agent/Dockerfile` + the per-task `worker_deps`
      list.
- [ ] Document the worker sandbox's "installed surface" inventory
      in `workers/agent/AGENT.md` so plan authors can grep it
      without reading the Dockerfile.

## Related

- `feedback_verify_binaries_exist_in_sandbox.md` (Claude memory) —
  the author-side discipline this learning crystallizes.
- ADR-0059 — per-repo `worker_deps` on RepoConfig; the structural
  fix at the agent-image scope.
- `docs/learnings/2026-05-27-validator-gate-references-host-binary.md`
  (if exists, the ADR-0058 sibling).
