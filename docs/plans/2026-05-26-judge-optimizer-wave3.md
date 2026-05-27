---
auto_merge: true
status: active
---

# Plan: ADR-0053 Wave 3 — schedule the optimizer + plumb corpus URI + operator-trigger CLI

- **Status:** active
- **Date:** 2026-05-26
- **Related ADRs:** ADR-0053 (agentic judge-prompt optimization), ADR-0035 (scheduler primitive)
- **Builds on:** Wave 2 (PR #17 — `role-prompt-optimizer` + `wf-tune-judge-prompts` seeded). Wave 1 eval harness already shipped (`workers/agent/treadmill_agent/judge_eval.py`).

## Goal

Make `wf-tune-judge-prompts` cron-runnable end-to-end + give operators a clean
manual trigger. Three pieces, two parallel tasks:

- **Task A** — plumb `TREADMILL_CORPUS_S3_URI` (deployment config → worker
  env) so the optimizer can pull the labeled corpus, and **seed the schedule**
  for `wf-tune-judge-prompts` (weekly, `role-architect` first).
- **Task B** — add a `treadmill workflows trigger <slug>` CLI + the matching
  API endpoint so operators can fire any workflow with a payload (also useful
  for any future scheduled bot we want to dry-run).

These compound Joe's "cron-runnable, reduces task-loop iterations" goal:
better judge prompts → fewer wf-feedback iterations per task → fewer worker
invocations per task.

## Success criteria

- Worker env carries `TREADMILL_CORPUS_S3_URI` whenever the deployment config
  has `corpus_s3_uri` set (a `local.yaml` or `aws` block field — match
  existing conventions).
- `schedules` row for `wf-tune-judge-prompts` is seeded (cron `0 20 * * 6` =
  Saturday 20:00 UTC, payload includes `repo` + `judge_role: role-architect`
  — **lesson from crystallization: `repo` MUST be in payload_template**).
- `treadmill workflows trigger wf-tune-judge-prompts --payload '{"repo":"…","judge_role":"role-architect"}'`
  POSTs to a new API endpoint, creates a taskless workflow_run, returns the
  run id; the worker picks it up + runs the optimizer.

## Constraints / scope

### In scope
Two parallel tasks (no `depends_on` between them — disjoint file scopes).

### Out of scope
- Multi-judge rotation (only `role-architect` for v1; later waves rotate or
  schedule per-judge).
- Updating the role-prompt-optimizer's prompt — Wave 2's prompt accepts
  `corpus_s3_uri` from the step payload; if env is also set, that's fine
  (worker can use either). No prompt edits needed.
- Operator-facing edits to `~/.treadmill/personal.yaml` — that's hand-driven
  after this merges. Plan calls it out as a follow-up step.

## sequence_of_work

```yaml
sequence_of_work:
  - id: wave3-corpus-and-schedule
    title: Plumb TREADMILL_CORPUS_S3_URI + seed wf-tune-judge-prompts schedule (ADR-0053 Wave 3)
    workflow: wf-author
    intent: |
      Two related changes (corpus URI plumbing + schedule seed) in one task,
      because both touch the deployment-config-to-worker-env pipeline.

      Read first:
        * ``tools/local-adapter/treadmill_local/deployment_config.py`` — find
          the schema validation (where ``webhook_inbox_queue_url`` etc. are
          declared) and the CDK-output mapping.
        * ``tools/local-adapter/treadmill_local/runtime.py`` ~line 754: where
          the API container env is built (``WEBHOOK_INBOX_QUEUE_URL`` etc.);
          ALSO find the worker-spawn site (search ``treadmill-agent`` container
          construction) — that's where the worker env is set.
        * ``services/api/treadmill_api/starters.py`` — find the existing
          schedule seeds (the ``wf-crystallize-learning`` schedule is at
          cron ``0 20 * * 0`` with ``payload_template``); add a parallel
          entry for ``wf-tune-judge-prompts`` (cron ``0 20 * * 6``, Saturday
          20:00 UTC, so it doesn't collide with crystallization).
        * [[project_schedule_payload_needs_repo]] — the schedule's
          ``payload_template`` MUST include ``repo`` or the dispatched
          ``step.ready`` event carries ``repo=""`` and workers hang silently.

      (1) DEPLOYMENT-CONFIG SCHEMA — add an OPTIONAL ``corpus_s3_uri: str |
      None = None`` field to whichever block matches: if the existing
      `aws_*` fields are under ``aws:``, add it under ``aws:`` (likely);
      otherwise pick the block that holds operator-visible URIs. Update the
      CDK-outputs mapping in ``deployment_config.py`` (the
      ``("aws_field", "CdkOutputName")`` tuples — ``corpus_s3_uri`` may not
      be a CDK output yet; if it's not, accept it as a user-supplied YAML
      field only and document that in the schema's docstring).

      (2) WORKER ENV PLUMBING — in ``runtime.py`` at the worker (treadmill-agent)
      spawn block where the env dict is assembled, add (when
      ``cfg["aws"].get("corpus_s3_uri")`` is set):
      ``env["TREADMILL_CORPUS_S3_URI"] = cfg["aws"]["corpus_s3_uri"]``.
      Skip if absent; do NOT raise. Mirror the existing pattern for
      ``WEBHOOK_INBOX_QUEUE_URL``.

      (3) SCHEDULE SEED — in ``starters.py``, add a schedule entry alongside
      the crystallization one:
        - ``cron_expression: "0 20 * * 6"`` (Saturday 20:00 UTC).
        - ``workflow_id: "wf-tune-judge-prompts"``.
        - ``payload_template: {"trigger": "scheduled-tune", "repo":
          "joeLepper/treadmill", "judge_role": "role-architect"}`` — MUST
          include ``repo`` (per the linked memory). Operators rotating to
          a different judge edit this seed or trigger manually via Task B.
        - ``jitter_seconds: 60``, ``status: 'active'``, ``created_by:
          'auto-seed'`` (mirror crystallization).
      The schedule is **idempotently seeded** via the operator CLI path
      (``seed_starters_if_empty`` is a no-op on a populated DB — see
      [[project_validation_gate_loop_pattern]] for the corrected analyzer
      finding). The plan ships the seed code; the operator runs
      ``treadmill workflows seed-starters`` after merge.

      (4) TESTS — exact-path test files (no ``pytest -k``):
        * ``tools/local-adapter/tests/test_corpus_uri_plumbing.py`` —
          construct a ``LocalRuntime`` with a deployment_config that
          includes ``corpus_s3_uri``; assert the worker env dict (the one
          passed to the agent container spawn) carries
          ``TREADMILL_CORPUS_S3_URI`` with the right value. And: when
          ``corpus_s3_uri`` is absent, the env dict does NOT carry the var
          (no empty string, no None).
        * ``services/api/tests/test_seed_optimizer_schedule.py`` — run the
          operator seed path; assert exactly one ``schedules`` row exists
          with ``workflow_id='wf-tune-judge-prompts'`` and
          ``payload_template`` containing ``repo`` AND ``judge_role``.

      (5) DOCS (ADR-0030 — REQUIRED): update
      ``tools/local-adapter/AGENT.md`` (the worker env now includes
      ``TREADMILL_CORPUS_S3_URI`` when ``aws.corpus_s3_uri`` is set in
      the deployment YAML) and ``services/api/AGENT.md`` (new schedule
      seeded; reference ADR-0053).
    scope:
      files:
        - tools/local-adapter/treadmill_local/deployment_config.py
        - tools/local-adapter/treadmill_local/runtime.py
        - services/api/treadmill_api/starters.py
        - tools/local-adapter/tests/test_corpus_uri_plumbing.py
        - services/api/tests/test_seed_optimizer_schedule.py
        - tools/local-adapter/AGENT.md
        - services/api/AGENT.md
      out_of_scope:
        - workers/agent/
        - services/api/treadmill_api/routers/
        - cli/
    validation:
      - kind: deterministic
        description: |
          Corpus URI plumbing + schedule seed both present; exact-path tests
          pass.
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          grep -q "corpus_s3_uri" "$ROOT/tools/local-adapter/treadmill_local/deployment_config.py" \
            && grep -q "TREADMILL_CORPUS_S3_URI" "$ROOT/tools/local-adapter/treadmill_local/runtime.py" \
            && grep -q "wf-tune-judge-prompts" "$ROOT/services/api/treadmill_api/starters.py" \
            && grep -q "0 20 \* \* 6\|'0 20 \* \* 6'\|\"0 20 \* \* 6\"" "$ROOT/services/api/treadmill_api/starters.py" \
            && [ -f "$ROOT/tools/local-adapter/tests/test_corpus_uri_plumbing.py" ] \
            && [ -f "$ROOT/services/api/tests/test_seed_optimizer_schedule.py" ] \
            && cd "$ROOT/tools/local-adapter" && uv run pytest tests/test_corpus_uri_plumbing.py -q \
            && cd "$ROOT/services/api" && uv run pytest tests/test_seed_optimizer_schedule.py -q

  - id: wave3-workflow-trigger-cli
    title: Operator CLI + API for triggering any workflow (ADR-0053 Wave 3)
    workflow: wf-author
    intent: |
      Add a clean way for operators to fire any workflow with a payload,
      independent of any task — useful for the optimizer's first manual run
      and any future scheduled-bot dry-run.

      Read first:
        * ``services/api/treadmill_api/routers/tasks.py`` — the ``retry``
          endpoint shape (the closest sibling); for the payload + 404/409
          response style.
        * ``services/api/treadmill_api/coordination/triggers.py`` —
          ``_create_and_publish_run_without_task`` (the function the
          scheduler uses to dispatch a taskless run); the new endpoint
          calls THIS function so we share one taskless-dispatch path.
        * ``cli/treadmill_cli/cli.py`` ``task_retry`` command + ``api_client.py``
          ``retry_task`` — mirror the shape exactly.

      (1) API — new ``services/api/treadmill_api/routers/workflow_triggers.py``
      (registered in ``routers/__init__.py``) — ``POST
      /api/v1/workflows/{workflow_slug}/trigger`` with body
      ``WorkflowTriggerRequest{payload: dict[str, Any]}``. Logic:
        - Look up the latest ``WorkflowVersion`` for ``workflow_slug``
          (404 if none).
        - Require ``payload`` to contain ``repo`` (400 otherwise, with a
          clear message — applies the lesson from
          [[project_schedule_payload_needs_repo]]).
        - Call ``_create_and_publish_run_without_task`` with the looked-up
          workflow_id + rendered_payload=body.payload. Trigger string:
          ``"operator:trigger"``.
        - Return ``{run_id: <uuid>, workflow_id: <slug>}`` 201.

      (2) CLIENT — ``api_client.py``: ``trigger_workflow(self,
      workflow_slug: str, payload: dict) -> dict``. Mirror ``retry_task``.

      (3) CLI — in ``cli/treadmill_cli/cli.py``, add a ``workflows trigger``
      command (the ``workflows_app`` group already exists from
      ``seed-starters``): ``treadmill workflows trigger <slug> --payload
      '<json>'`` — parses ``--payload`` as JSON, prints
      ``triggered: workflow_run=<id>`` on success; 404 → "workflow not
      found"; 400 → "payload missing required field: …"; non-zero exit.

      (4) TESTS — exact-path test files:
        * ``services/api/tests/test_workflow_trigger_endpoint.py`` —
          create a workflow version in a fixture DB; POST with valid
          payload → 201 + workflow_run exists; missing ``repo`` → 400;
          unknown workflow → 404.
        * ``cli/tests/test_cli_workflows_trigger.py`` — invoke
          ``workflows trigger`` against a stubbed client; assert payload
          parsing + the output line. Test the 404 + 400 paths.

      (5) DOCS (ADR-0030 — REQUIRED): update
      ``services/api/AGENT.md`` (new endpoint under "Key surfaces") + the
      CLI reference doc if one exists.
    scope:
      files:
        - services/api/treadmill_api/routers/workflow_triggers.py
        - services/api/treadmill_api/routers/__init__.py
        - cli/treadmill_cli/api_client.py
        - cli/treadmill_cli/cli.py
        - services/api/tests/test_workflow_trigger_endpoint.py
        - cli/tests/test_cli_workflows_trigger.py
        - services/api/AGENT.md
      out_of_scope:
        - services/api/treadmill_api/coordination/triggers.py
        - services/api/treadmill_api/starters.py
        - tools/local-adapter/
        - workers/agent/
    validation:
      - kind: deterministic
        description: |
          The trigger endpoint + CLI exist with the expected functions and
          their dedicated test files pass.
        script: |
          ROOT="$(git rev-parse --show-toplevel)"
          grep -q "/workflows/.*/trigger\|workflow_slug.*trigger" "$ROOT/services/api/treadmill_api/routers/workflow_triggers.py" \
            && grep -q "def trigger_workflow" "$ROOT/cli/treadmill_cli/api_client.py" \
            && grep -qE "workflows_app.command.*trigger|trigger.*command" "$ROOT/cli/treadmill_cli/cli.py" \
            && [ -f "$ROOT/services/api/tests/test_workflow_trigger_endpoint.py" ] \
            && [ -f "$ROOT/cli/tests/test_cli_workflows_trigger.py" ] \
            && cd "$ROOT/services/api" && uv run pytest tests/test_workflow_trigger_endpoint.py -q \
            && cd "$ROOT/cli" && uv run pytest tests/test_cli_workflows_trigger.py -q
```

## Risks / unknowns

- **Operator must update `~/.treadmill/personal.yaml`** with
  `aws.corpus_s3_uri: s3://treadmill-analysis-corpus-…/docs/analysis/`
  AFTER the merge. The plan ships the *plumbing*; the *value* is operator
  hands. Until that's set + the API recreates, the worker env won't carry
  `TREADMILL_CORPUS_S3_URI` and the scheduled optimizer fire will fail to
  pull the corpus.
- **Workflow_trigger endpoint reuses the taskless-dispatch path** — keep
  this in mind for any future audit that "only the scheduler dispatches
  taskless runs"; this becomes a *second* operator-driven taskless path.
  That's fine but worth a note in the endpoint's docstring.
- **First Saturday fire is 2026-05-30 20:00 UTC** — gives 3+ days for the
  operator setup + a manual dry-run via Task B's CLI before the cron hits.

## Post-mortem

_(filled when the wave completes)_
