---
auto_merge: false
---

# Plan: ADR-0059 Step 6 — materialization wiring integration + canary runbook

- **Status:** completed
- **Date:** 2026-05-28
- **Related ADRs:** ADR-0059 (per-repo worker-dep registration —
  Steps 1, 2, 4, 5 shipped), ADR-0060 (egress proxy — gates the
  live canary runbook)
- **Supersedes:** none
- **Depends on:** ADR-0059 Steps 1, 2, 4, 5 (shipped). The
  runbook's *operator-driven* canary requires ADR-0060 Step 3 to
  land first; the *pytest* integration test does not.

## Goal

Close ADR-0059 with two deliverables that prove the worker_deps
chain composes correctly end-to-end, complementing the per-module
unit tests that ship today:

1. A pytest wiring assertion that exercises the actual seam between
   `repo_deps.materialize` → `repo_deps.bind_overlay` → the
   ContextVar lookup in `validation_runtime` → the subprocess env
   the validation gate receives. The existing
   `workers/agent/tests/test_repo_deps.py` covers `materialize()`
   in isolation; this test crosses the module boundary so a future
   refactor that breaks the ContextVar handoff fails loudly.
2. A canary runbook describing the dev-local operator procedure for
   the *live* end-to-end smoke (real PyPI, real `pip install`, real
   overlay on disk, real cache reuse), runnable once ADR-0060 Step
   3 (egress proxy) lands so the install phase has the allowlist.

`auto_merge: false` — concurrent operator on the RAMJAC unblock
track; manual merge keeps the orchestration clean.

## Success criteria

- `workers/agent/tests/test_repo_deps_integration.py` exists and
  contains at least:
  - A wiring test that:
    - Calls `materialize()` with a small synthetic `WorkerDeps`
      (Python-only is sufficient), with `subprocess.run` patched
      at the `treadmill_agent.repo_deps` boundary so the venv +
      pip steps return success without actual network.
    - Binds the returned overlay via `bind_overlay()`.
    - Patches `subprocess.run` at the
      `treadmill_agent.validation_runtime` boundary; invokes
      `validation_runtime.run_deterministic` with a no-op script.
    - Asserts the `env` kwarg passed to the validation subprocess
      contains a `PATH` value whose first segments include the
      overlay's `venv/bin` path, and a `PYTHONPATH` pointing at
      the overlay's `site-packages`.
    - Calls `reset_overlay()` and re-invokes `run_deterministic`;
      asserts the env no longer carries the overlay paths.
  - A failure-propagation test that:
    - Patches `subprocess.run` to raise on the pip-install step.
    - Calls `materialize()` and asserts
      `WorkerDepsMaterializationError` with `stage='python'`.
- `docs/runbooks/2026-05-28-worker-deps-canary.md` exists and
  describes the dev-local operator procedure to:
  - Register a canary repo (any throwaway test repo) with
    `treadmill onboarding update <repo> --worker-deps-python
    packaging==24.0`.
  - Dispatch a no-op task against that repo.
  - Confirm the worker's stdout contains a `repo_deps cache miss`
    line on the first run + `repo_deps cache hit` on a second
    dispatch.
  - Confirm `/var/treadmill/repo-overlays/<slug>/.deps-hash` exists
    inside the worker container (`docker exec` example included).
  - Note explicitly that the runbook **gates on ADR-0060 Step 3
    landing** — without the egress proxy, the install phase has
    no allowlisted PyPI route.
- `workers/agent/AGENT.md` cross-references the new integration
  test + the runbook, per ADR-0030.

## Constraints / scope

### In scope

- The wiring integration test file + the canary runbook + the
  AGENT.md cross-reference.

### Out of scope

- Modifying `repo_deps.py` or `validation_runtime.py` —
  Step 6 asserts the existing wiring, doesn't change it. If the
  wiring assertion uncovers a real bug, raise a follow-up task.
- A wheelhouse-based "real pip install" test fixture. Considered
  and rejected for v1 — pip's build backend resolution makes a
  hermetic local-wheel test fragile to maintain, and the wiring
  test + the live canary already cover both ends of the
  determinism spectrum.
- Re-testing repo_deps unit behavior (already covered in
  `test_repo_deps.py`).

### Budget

One worker dispatch. Two new files, one small AGENT.md edit.

## Sequence of work

```yaml
sequence_of_work:
  - id: adr-0059-step-6-wiring-integration-and-canary-runbook
    title: "ADR-0059 Step 6 — wiring integration test + canary runbook"
    workflow: wf-author
    intent: |
      STUDY: read these as shape references —
        - `workers/agent/treadmill_agent/repo_deps.py` — the
          `materialize()` + `bind_overlay()` + `current_overlay()`
          + `env_overrides()` surface.
        - `workers/agent/treadmill_agent/validation_runtime.py` —
          the subprocess.run site that reads
          `current_overlay()` and merges `env_overrides()`. Line
          ~90 is where the ContextVar lookup happens.
        - `workers/agent/tests/test_repo_deps.py` — the existing
          unit tests' mock pattern (patch
          `treadmill_agent.repo_deps.subprocess.run`). The new
          integration test patches at a DIFFERENT module
          boundary: `treadmill_agent.validation_runtime.subprocess.run`
          for the validation invocation, and the repo_deps
          boundary for the install steps. Two separate patch
          contexts in the same test.
        - `docs/runbooks/edit-a-role-prompt.md` — the runbook
          style (TL;DR block at top, mental model, step-by-step,
          verify commands).

      BUILD #1: `workers/agent/tests/test_repo_deps_integration.py`
      with at least two tests:

        - `test_wiring_overlay_env_reaches_validation_subprocess`:
          - Construct a small WorkerDeps with python=["packaging==24.0"].
          - Patch `treadmill_agent.repo_deps.subprocess.run` to
            return success (no real install). Call
            `repo_deps.materialize("test/repo", worker_deps,
            overlay_root=tmp_path)`. Assert the returned overlay
            has `venv_path is not None` and `fresh=True`.
          - Bind via `bind_overlay(overlay)`.
          - Patch `treadmill_agent.validation_runtime.subprocess.run`
            to a Mock that returns a CompletedProcess; call the
            existing `run_deterministic` entry point with a no-op
            shell script.
          - Assert the captured `env` kwarg has `PATH` starting
            with the overlay's `venv/bin` path; `PYTHONPATH`
            contains the overlay's site-packages.
          - Call `reset_overlay(token)`. Re-invoke
            `run_deterministic`; assert the captured `env` no
            longer has the overlay paths in PATH.

        - `test_wiring_failure_propagates_as_materialization_error`:
          - Same WorkerDeps shape.
          - Patch `treadmill_agent.repo_deps.subprocess.run` so the
            second call (pip install) raises CalledProcessError.
          - Call `materialize()` and assert
            `WorkerDepsMaterializationError` with `stage="python"`.

      Module docstring explains: this file complements
      `test_repo_deps.py` (which covers materialize in isolation)
      by crossing the repo_deps → validation_runtime module
      boundary so a future ContextVar-handoff regression fails
      here.

      BUILD #2: `docs/runbooks/2026-05-28-worker-deps-canary.md`
      with sections:

        - TL;DR (5-line command sequence: onboarding update →
          dispatch a no-op task → watch worker logs → verify
          cache hit on second dispatch → verify overlay dir).
        - Mental model (cross-references ADR-0059 + ADR-0060,
          explains the install vs task phase contract).
        - Procedure (numbered, dev-local-specific):
          1. Verify ADR-0060 Step 3 has landed
             (`docker exec treadmill-egress-proxy ...` health
             check).
          2. Register canary worker_deps via the CLI.
          3. Dispatch a no-op task against the canary repo.
          4. Tail the worker container's stdout; expect
             `repo_deps cache miss` then a `pip install` log.
          5. Re-dispatch the same task; expect `repo_deps cache
             hit` and no install log this time.
          6. `docker exec <worker> ls -l /var/treadmill/repo-overlays`
             confirms the overlay dir + `.deps-hash` file.
        - Failure modes (what to look for in the worker stdout if
          materialize errors; how `TaskWorkerDepsFailed` event
          surfaces in the dashboard).
        - Pointers (ADR-0059, ADR-0060, the integration test
          file).

      BUILD #3: edit `workers/agent/AGENT.md` to add a
      Cross-references section entry (or extend an existing one)
      that names the new test file + the runbook, per ADR-0030.
    scope:
      files:
        - workers/agent/tests/test_repo_deps_integration.py
        - docs/runbooks/2026-05-28-worker-deps-canary.md
        - workers/agent/AGENT.md
      services_affected:
        - workers/agent
      out_of_scope:
        - workers/agent/treadmill_agent/repo_deps.py
        - workers/agent/treadmill_agent/validation_runtime.py
        - workers/agent/treadmill_agent/runner.py
        - workers/agent/tests/test_repo_deps.py
    validation:
      - kind: deterministic
        description: |
          The new integration test file passes.
        script: |
          uv run --package treadmill-agent pytest workers/agent/tests/test_repo_deps_integration.py -q
      - kind: deterministic
        description: |
          The integration test file exercises both the wiring +
          failure paths.
        script: |
          grep -lE "def test_wiring_overlay_env_reaches_validation_subprocess|def test_wiring_failure_propagates_as_materialization_error" workers/agent/tests/test_repo_deps_integration.py
      - kind: deterministic
        description: |
          The canary runbook exists and references both ADR-0059
          and ADR-0060.
        script: |
          grep -lE "ADR-0059" docs/runbooks/2026-05-28-worker-deps-canary.md
          grep -lE "ADR-0060" docs/runbooks/2026-05-28-worker-deps-canary.md
      - kind: deterministic
        description: |
          AGENT.md cross-references the new test file + runbook.
        script: |
          grep -lE "test_repo_deps_integration|worker-deps-canary" workers/agent/AGENT.md
```

## Diagram

Not applicable — Step 6 asserts existing wiring shipped in Steps 2 +
4, doesn't introduce new components. The architecture diagram lives
in ADR-0059.

## Risks / unknowns

- **The wiring test's patch boundary is subtle.** `repo_deps` patches
  at `treadmill_agent.repo_deps.subprocess.run`; `validation_runtime`
  patches at `treadmill_agent.validation_runtime.subprocess.run`.
  Mixing them in one test requires careful import + patch ordering.
  Mitigation: each patch is in its own `with` context, scoped to the
  call it's gating. Worker should test-run the file locally before
  flagging done.
- **The canary runbook references commands that depend on ADR-0060
  Step 3 (egress proxy) landing.** The runbook explicitly gates on
  that, so attempting it before Step 3 lands surfaces a clear
  prerequisite-not-met error rather than a silent confusion.
- **AGENT.md update may conflict** with concurrent edits from other
  active worker tasks. Mitigation: `auto_merge: false` for this
  plan + manual merge with rebase if conflict surfaces.

## Decisions captured during execution

(empty at draft time; appended as work progresses)

## Post-mortem

(filled when plan transitions to completed)
