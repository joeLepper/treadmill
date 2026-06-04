---
auto_merge: false
---

# Plan: ADR-0059 Steps 4 + 5 — worker-deps-failed event + operator CLI

- **Status:** completed
- **Date:** 2026-05-28
- **Related ADRs:** ADR-0059, ADR-0058 (gate-broken — sibling
  escalation pattern this borrows from)
- **Depends on:** ADR-0059 Step 1 (#62), Step 2 (#63) — both merged

## Goal

Ship the two remaining "small + mechanical" steps of ADR-0059:

* **Step 4:** add `task.worker_deps_failed` event payload + emit it
  from the worker's materialization-error path. Surfaces install
  failures to the operator with the same shape as the ADR-0058
  gate-broken escalation.

* **Step 5:** add operator CLI flags to set `worker_deps` on a repo
  (`treadmill onboarding update <repo> --worker-deps-python ...
  --worker-deps-node ... --binary name=URL=SHA256@TARGET`) so an
  operator can register deps without raw API calls. This is the
  load-bearing user-facing step — without it, ADR-0059's value is
  locked behind `curl` invocations.

Both tasks are independent (no `depends_on`) so workers can pick them
up in parallel. `auto_merge: false` for the same concurrent-orchestrator
discipline as the prior ADR-0058/ADR-0059 plans.

Self-checked against `.claude/skills/plan/SKILL.md` worker-sandbox +
robustness rules per
[[feedback_apply_skill_md_rules_to_my_own_plans]] (banked 2026-05-28
after I shipped two violations in the same morning's plan-authoring):

- No `cdk synth`, `docker`, `aws ...`, `psql`, `alembic upgrade`,
  `pip install`, `npm install`, or `curl` against non-local URLs in
  any validation script.
- No filename pinning in deterministic gates (content-grep only).
- Tests scoped to focused pytest paths that respect each repo's
  fixtures.

## Success criteria

- A `TaskWorkerDepsFailed` event payload exists with `task_id`, `repo`,
  `stage: Literal["python", "node", "binary"]`, `detail: str`,
  `worker_deps_hash: str`.
- The worker's `repo_deps.materialize` exception handler catches
  `WorkerDepsMaterializationError` and emits the new event via the
  existing event-publish seam. Step.failed still fires alongside
  (the worker exits the step cleanly so the run can be retried after
  the operator fixes the registration).
- `treadmill onboarding update <repo> --worker-deps-python aws-cdk-lib==2.214.0`
  works end-to-end: CLI builds a partial WorkerDeps, fetches the
  current RepoConfig, merges (additive — empty lists on absent flags
  preserve existing values), POSTs back. `--clear-worker-deps`
  flag empties everything.
- `--binary name=URL=SHA256@TARGET` syntax parses correctly into a
  `BinarySpec`; invalid checksums / target paths surface the
  Pydantic validation error to stderr with a non-zero exit.

## Constraints / scope

### In scope

- New event payload `TaskWorkerDepsFailed` (Step 4).
- Emit-on-materialize-failure hook in the worker (Step 4).
- CLI subcommand: `treadmill onboarding update <repo> [flags]` (Step 5).
- Tests for both: payload validation, emit path, CLI argument
  parsing, additive merge.
- AGENT.md updates for both services/api + cli per ADR-0030.

### Out of scope

- Egress scoping (Step 3 — separate plan, not yet authored).
- Integration smoke against real PyPI / npm (Step 6 — separate plan).
- Wire-up of `task.worker_deps_failed` into ADR-0058's gate-broken
  classifier (the classifier reads validation stderr today; worker_deps
  failures are a separate surface and don't need the classifier hook
  for v1 — they're already operator-visible via the new event).

### Budget

Two worker dispatches, ~1 PR each. Both should land first-try given
the self-check applied; if either wedges, the meta-feedback is more
valuable than the specific work.

## Sequence of work

```yaml
sequence_of_work:
  - id: adr-0059-step-4-worker-deps-failed-event
    title: "ADR-0059 Step 4 — task.worker_deps_failed event + emit-on-materialize-failure"
    workflow: wf-author
    intent: |
      STUDY: read these as shape references —
        - `services/api/treadmill_api/events/task.py` for the
          existing `TaskEscalatedToOperator` (added by ADR-0058 Step 3)
          and `TaskCancelled` shapes. The new event follows the same
          pattern.
        - `workers/agent/treadmill_agent/repo_deps.py` from Step 2
          (PR #63) — find the `WorkerDepsMaterializationError`
          definition and the materialize() exception path.
        - `workers/agent/treadmill_agent/runner.py` for where
          materialize() is called (the per-step seam after the App
          re-mint). The error currently propagates and fails the
          step; we keep that behavior + add the event emission
          alongside.
        - The event-publish helper the worker already uses for
          step.failed / step.completed events (find via grep).

      BUILD:

      (1) New `TaskWorkerDepsFailed` payload in
          `services/api/treadmill_api/events/task.py`:

          ```python
          class TaskWorkerDepsFailed(EventPayload):
              """Emitted when ``repo_deps.materialize`` raises
              WorkerDepsMaterializationError. The operator surface
              (dashboard escalations list) gets a distinct event vs
              gate-broken / architect_cap / stuck_task_sweep."""
              ENTITY_TYPE: ClassVar[str] = "task"
              ACTION: ClassVar[str] = "worker_deps_failed"
              task_id: uuid.UUID
              repo: str
              stage: Literal["python", "node", "binary"]
              detail: str  # the exception's detail field, the stderr or checksum-mismatch line
              worker_deps_hash: str  # for cache-correlation
          ```
          Register it in the events `__init__.py` so it's importable
          from `treadmill_api.events`.

      (2) Wire-in at the worker materialize-failure site:

          In `workers/agent/treadmill_agent/runner.py` (or wherever
          materialize() is called per-step), wrap the call:

          ```python
          try:
              overlay = materialize(ctx.repo, worker_deps)
          except WorkerDepsMaterializationError as exc:
              # Emit the typed event, then re-raise so step.failed
              # still fires (the run can retry once the operator
              # fixes the registration; the typed event is the
              # operator-visible signal that something needs
              # attention).
              await event_publisher.publish_task_worker_deps_failed(
                  task_id=ctx.task_id,
                  repo=ctx.repo,
                  stage=exc.stage,
                  detail=exc.detail,
                  worker_deps_hash=compute_deps_hash(worker_deps),
              )
              raise
          ```
          (Use the existing EventPublisher's API; if no method exists
          for the new payload, add it as a thin wrapper that builds
          the Pydantic envelope + publishes via the same channel
          step.failed uses.)

      TESTS:
      Add `services/api/tests/test_task_worker_deps_failed_event.py`:
        - Payload validates with all required fields.
        - Payload rejects an invalid stage value.
        - Empty detail is rejected (min_length=1 on the field).
        - Round-trips through `event.model_dump_json()` and back.
      Add coverage in `workers/agent/tests/test_runner_dispositions.py`
      (extend existing tests) for the materialize-failure → emit
      path: mock materialize() to raise WorkerDepsMaterializationError;
      assert the event publisher's publish_task_worker_deps_failed
      was called once with the right args; assert the exception
      propagates so step.failed still fires.

      DOC:
        - `services/api/AGENT.md`: Recent-changes entry citing
          ADR-0059 Step 4.
        - `workers/agent/AGENT.md`: extend the repo_deps key-surfaces
          line to mention the failure-event emission.

      Self-checked against SKILL.md rules: deterministic gate uses
      pytest only, no sandbox-unsafe tools, content-grep robustness.
    scope:
      files:
        - services/api/treadmill_api/events/task.py
        - services/api/treadmill_api/events/__init__.py
        - services/api/tests/test_task_worker_deps_failed_event.py
        - workers/agent/treadmill_agent/runner.py
        - workers/agent/tests/test_runner_dispositions.py
        - services/api/AGENT.md
        - workers/agent/AGENT.md
      services_affected:
        - services/api
        - workers/agent
      out_of_scope:
        - Wiring the new event into the dashboard escalations bucket
          (separate dashboard-track task)
        - Hooking into ADR-0058's gate-broken classifier (the
          classifier path handles validation stderr; worker_deps
          failures are a separate surface)
    validation:
      - kind: deterministic
        description: New event-payload tests pass (services/api side).
        script: |
          cd services/api && uv run pytest tests/test_task_worker_deps_failed_event.py -q
        severity: blocking
        timeout_seconds: 120
      - kind: deterministic
        description: Worker-side runner-disposition tests stay green (extended for the materialize-failure → emit path).
        script: |
          cd workers/agent && uv run pytest tests/test_runner_dispositions.py -q
        severity: blocking
        timeout_seconds: 120
      - kind: deterministic
        description: New event payload class is defined and exported from treadmill_api.events.
        script: |
          grep -lE 'class TaskWorkerDepsFailed' services/api/treadmill_api/events/*.py | head -1 && grep -qE 'TaskWorkerDepsFailed' services/api/treadmill_api/events/__init__.py
        severity: blocking
        timeout_seconds: 30
      - kind: llm-judge
        description: AGENT.md updates per ADR-0030 on both touched services.
        prompt: |
          The DIFF should include Recent-changes entries OR Key-surfaces extensions
          in BOTH services/api/AGENT.md AND workers/agent/AGENT.md citing
          ADR-0059 Step 4. Return verdict 'pass' when both are present; 'fail'
          otherwise.
        severity: blocking

  - id: adr-0059-step-5-operator-cli
    title: "ADR-0059 Step 5 — treadmill onboarding update <repo> --worker-deps-* CLI flags"
    workflow: wf-author
    intent: |
      STUDY: read these as shape references —
        - `cli/treadmill_cli/` directory layout — find the onboarding
          subcommand module (likely `onboarding.py`); read how
          existing subcommands accept flags, build request bodies,
          and call `ApiClient` methods.
        - `cli/treadmill_cli/api_client.py` for the
          `ApiClient.upsert_repo_config` (or equivalent) method
          shape. If no such method exists, add one that POSTs to
          `/api/v1/onboarding/repos` with the new fields.
        - `services/api/treadmill_api/models/onboarding.py` for the
          `WorkerDeps` / `BinarySpec` shapes (Step 1 / PR #62).
        - `services/api/treadmill_api/repo_config.py` for the
          `RepoConfig` dataclass with the `worker_deps` field
          (Step 1 added it).

      BUILD:

      Add a CLI subcommand `treadmill onboarding update <repo>` with
      these flags:

        --worker-deps-python <spec>      (repeatable; e.g.
                                          --worker-deps-python aws-cdk-lib==2.214.0
                                          --worker-deps-python constructs==10.3.0)
        --worker-deps-node <spec>        (repeatable)
        --binary name=URL=SHA256@TARGET  (repeatable; the four-tuple
                                          parses into a BinarySpec)
        --clear-worker-deps              (sets all three lists to empty;
                                          mutually exclusive with the others)

      Behavior:
        - Fetch the current RepoConfig via
          `ApiClient.get_repo_config(repo)`. If 404, fail with a
          clear "repo not registered yet — run `treadmill onboarding
          add` first" message; do NOT auto-register.
        - Build the merged WorkerDeps:
            - Without --clear-worker-deps: ADDITIVE. The flag values
              are appended to the existing lists (deduplicated by
              exact-string match). Use a small helper
              `_merge_worker_deps(existing, new_python, new_node,
              new_binaries) -> WorkerDeps`.
            - With --clear-worker-deps: REPLACE everything with empty
              lists (other flags rejected with an error).
        - POST the updated RepoConfig back via
          `ApiClient.upsert_repo_config(...)`.
        - On Pydantic validation error (bad checksum, wrong target_path
          prefix), surface the error to stderr with exit code 1.
        - Print a one-line summary: "updated worker_deps for <repo>:
          python=N node=M binaries=K".

      The --binary syntax: `name=URL=SHA256@TARGET`. Parse with
      `.rsplit('@', 1)` to peel TARGET; then `.split('=', 2)` to peel
      name + URL + SHA256. Reject malformed entries clearly.

      TESTS in `cli/tests/test_onboarding_update.py`:
        - --worker-deps-python adds new specs (additive against
          existing).
        - --binary syntax parses correctly (happy + error cases).
        - --clear-worker-deps empties everything.
        - --clear-worker-deps + --worker-deps-python rejected
          mutually-exclusive.
        - 404 from get_repo_config surfaces clear error message.
        - Pydantic validation error from API surfaces with exit 1.

      DOC:
        - `cli/AGENT.md` (or `cli/treadmill_cli/AGENT.md` — check
          which exists): Recent-changes entry citing ADR-0059 Step 5.

      Self-checked against SKILL.md rules: deterministic gate uses
      pytest only, no sandbox-unsafe tools, content-grep robustness
      (no filename pin; the grep targets the new flag name).
    scope:
      files:
        - cli/treadmill_cli/onboarding.py
        - cli/treadmill_cli/api_client.py
        - cli/tests/test_onboarding_update.py
        - cli/AGENT.md
      services_affected:
        - cli
      out_of_scope:
        - Egress scoping (Step 3)
        - wf-discover changes to auto-detect deps (separate plan)
    validation:
      - kind: deterministic
        description: CLI tests including the new onboarding update coverage pass.
        script: |
          cd cli && uv run pytest tests/test_onboarding_update.py -q
        severity: blocking
        timeout_seconds: 120
      - kind: deterministic
        description: New --worker-deps-python flag exists in the CLI source.
        script: |
          grep -lE 'worker-deps-python|worker_deps_python' cli/treadmill_cli/*.py | head -1
        severity: blocking
        timeout_seconds: 30
      - kind: llm-judge
        description: AGENT.md updated per ADR-0030.
        prompt: |
          The DIFF should include either a Recent-changes entry or a Key-surfaces
          extension in an AGENT.md file under cli/ (cli/AGENT.md or sibling)
          citing ADR-0059 Step 5. Return verdict 'pass' when present; 'fail'
          otherwise.
        severity: blocking
```

## Risks / unknowns

- **CLI scaffolding location.** The plan's STUDY step has the worker
  discover where the onboarding subcommand lives; if it doesn't yet
  exist as a module, the worker creates it. Either way, the task is
  bounded by `scope.files`.
- **`upsert_repo_config` may need to be added to ApiClient.** If the
  existing client doesn't have a method for the onboarding endpoint,
  the task adds it. Mention this in the STUDY block.
- **We'll abort if** either task burns its 5-attempt cap (real signal
  worth investigating). Same discipline as Step 1's retry cycle —
  cancel + audit the plan + re-dispatch, but stop after 3 attempts
  per task.

## Decisions captured during execution

(empty)

## Post-mortem

(filled in on completion / abandonment)
