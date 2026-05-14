---
status: active
trigger: ADR-0024 accepted 2026-05-13. Task 1 (deploy-events-queue-cdk) shipped via PR #23 on 2026-05-13. Chain stalled when session shifted to ralph-loop / ADR-0029 work. Re-firing 2026-05-14 with task 1 trimmed + validation scripts added per migration 0011's CHECK constraint. 4 remaining tasks: init wire, watcher module, spawn-on-up, smoke+docs. Need this shipped because the running API doesn't pick up new main commits without manual ``treadmill-local down/up`` — the friction is real (the same task #120 fix we just merged hasn't taken effect yet).
parent: docs/plans/2026-05-13-week-4-dev-local-deployment.md
---

# Plan: auto-redeploy watcher per ADR-0024

ADR-0024 commits to a new host-side subprocess that watches for
`pr_merged` events and rebuilds + cycles local containers on
relevant code changes. This plan implements it.

`status: drafting` — operator review pass before flipping to active.

## Goal

After this plan executes:

1. CDK provisions a new SQS queue `treadmill-<deployment_id>-deploy-events`
   subscribed to the events SNS topic with a filter limiting it to
   `entity_type=github, action=pr_merged`. A DLQ pairs it.
2. `treadmill-local init` reads the new queue URLs from CFN outputs
   into the per-deployment YAML.
3. `treadmill-local up --deployment <id>` spawns a new
   `deploy-watcher` subprocess alongside the autoscaler. The
   subprocess polls the deploy-events queue on a 10s tick.
4. On a `pr_merged` event, the watcher fetches changed files via
   gh API, categorizes them against the dispatch table from
   ADR-0024, and executes per category:
   - `services/api/**` → rebuild `treadmill-api:dev` + restart
     container
   - `workers/agent/**` → rebuild `treadmill-agent:dev` (no restart)
   - `infra/**` → notify only
   - `tools/local-adapter/**` → notify only
   - other → no-op
5. Per-category last-applied SHA tracked in
   `.treadmill-local/deploy-watcher-state.json` for idempotency.
6. `--no-deploy-watcher` flag on `up` opts out (mirroring
   `--no-autoscaler`).

## Constraints / scope

### In scope

- New SQS queue + DLQ + SNS subscription via CDK.
- New `deploy-watcher.py` module in `tools/local-adapter/`.
- Dispatch table + per-category actions per ADR-0024.
- PID + log file lifecycle (mirrors autoscaler).
- Idempotency state file.
- `--no-deploy-watcher` flag.
- Tests.

### Out of scope

- The trigger-registry consolidation (deferred per ADR-0024; happens
  when the fourth `pr_merged` trigger lands).
- Auto-deploying `infra/**` changes (notify-only at v0).
- Self-rebuilding the deploy-watcher when local-adapter code changes
  (operator-mediated at v0).
- Observability of the watcher itself (banked for ADR-0020's o11y
  stack — Q24.b).
- PR-comment-on-rebuild-failure (Q24.a — defer).

## Sequence of work

```yaml
sequence_of_work:
  - id: treadmill-local-init-deploy-events
    title: treadmill-local init reads deploy-events CFN outputs into the YAML
    workflow: wf-author
    intent: |
      Extend ``tools/local-adapter/treadmill_local/deployment_config.py``
      to read ``DeployEventsQueueUrl`` and ``DeployEventsDlqUrl``
      from the deployed stack's CFN outputs and write them into the
      per-deployment YAML under ``aws.deploy_events_queue_url`` and
      ``aws.deploy_events_dlq_url``.

      Tests:
      - A fixture with the new CFN outputs serializes to a YAML that
        carries both new keys.
      - Loading an older YAML (without the new keys) raises a clear
        error — the watcher needs both keys to operate.
    scope:
      files:
        - tools/local-adapter/treadmill_local/deployment_config.py
        - tools/local-adapter/tests/test_deployment_config.py
    validation:
      - kind: deterministic
        description: |
          A deployment config built from CFN outputs including
          ``DeployEventsQueueUrl=...`` serializes with
          ``aws.deploy_events_queue_url`` set to that value.

        script: |
          cd tools/local-adapter && uv run pytest tests/test_deployment_config.py -q \
            && grep -q "deploy_events_queue_url" treadmill_local/deployment_config.py
  - id: deploy-watcher-module
    title: Deploy watcher module + dispatch table + state file
    workflow: wf-author
    depends_on:
      - task.treadmill-local-init-deploy-events.pr_merged
    intent: |
      New module ``tools/local-adapter/treadmill_local/deploy_watcher.py``
      with the watcher class + ``main()`` subprocess entrypoint
      (mirroring ``autoscaler.py``'s structure).

      The watcher:
      - Reads deploy_events_queue_url from the deployment config.
      - Long-polls SQS (``WaitTimeSeconds=20``, ``MaxNumberOfMessages=1``).
      - On each message:
        * Parse the envelope (it's an SNS-wrapped Treadmill event).
        * Extract pr_number + merge_commit_sha from the payload.
        * Check the state file for last-applied SHA per category;
          if the incoming SHA matches an applied SHA, ack + skip.
        * Fetch changed files via gh API:
          ``GET /repos/<owner>/<repo>/pulls/<pr_number>/files``.
          Use the GITHUB_TOKEN from env (per ADR-0019).
        * Categorize each file against the dispatch table:
          - ``services/api/**`` → api category
          - ``workers/agent/**`` → agent category
          - ``infra/**`` → infra (notify-only)
          - ``tools/local-adapter/**`` → adapter (notify-only)
          - other → ignored
        * For each non-empty category, run the action:
          - api: ``docker build -t treadmill-api:dev <repo-root>/services/api``
            then ``docker restart treadmill-api``. Verify the
            container reports healthy on /health/ready within 30s.
          - agent: ``docker build -t treadmill-agent:dev
            <repo-root> -f workers/agent/Dockerfile``. No restart
            (workers are one-shot per ADR-0018).
          - infra: print a structured notification message naming
            the affected files; do NOT shell out.
          - adapter: same — print a notification.
        * On success, update the state file with the SHA.
        * Ack the SQS message.
      - On failure: log, do NOT ack (let SQS re-deliver per
        ``maxReceiveCount=3``; goes to DLQ after).

      Dispatch ordering matters: the first glob-match wins, so the
      table is iterated in the order above (api before agent before
      infra etc.) — files in ``infra/observability/`` would match
      infra, not api.

      Tests:
      - Each category's action invoked correctly when its glob
        matches (mocked subprocess for ``docker build``).
      - State-file idempotency: a re-delivered event for the same
        SHA + category skips the rebuild.
      - Dispatch ordering: a file in
        ``infra/observability/dashboards/`` is categorized infra,
        not api.
      - gh API mocked; the watcher handles 404 (PR deleted) gracefully.
    scope:
      files:
        - tools/local-adapter/treadmill_local/deploy_watcher.py
        - tools/local-adapter/tests/test_deploy_watcher.py
    validation:
      - kind: deterministic
        description: |
          Stub ``boto3.client("sqs")`` to deliver one synthetic
          ``pr_merged`` message; stub the gh API; stub
          ``subprocess.run``; the watcher rebuilds the right images
          per category and acks the message.
        script: |
          cd tools/local-adapter && uv run pytest tests/test_deploy_watcher.py -q \
            && test -f treadmill_local/deploy_watcher.py
      - kind: deterministic
        description: |
          Stub a second delivery with the same SHA; the watcher
          checks the state file and skips the rebuild.
        script: |
          cd tools/local-adapter && uv run pytest tests/test_deploy_watcher.py -q

  - id: deploy-watcher-spawn-on-up
    title: treadmill-local up spawns the deploy watcher
    workflow: wf-author
    depends_on:
      - task.deploy-watcher-module.pr_merged
    intent: |
      In ``tools/local-adapter/treadmill_local/runtime.py``, add
      ``_start_deploy_watcher_dev_local`` (mirrors
      ``_start_autoscaler_dev_local``):

      - Spawn ``python -m treadmill_local.deploy_watcher`` as a
        detached subprocess.
      - Env: ``TREADMILL_DEPLOY_WATCHER_DEPLOYMENT_ID=<id>`` so the
        subprocess loads the right per-deployment YAML.
      - Inherit AWS_PROFILE + GITHUB_TOKEN from the operator's
        shell (same pattern as the autoscaler).
      - PID file at ``.treadmill-local/deploy-watcher.pid``.
      - Log file at ``.treadmill-local/deploy-watcher.log``.

      Add ``_stop_deploy_watcher`` (SIGTERM + cleanup; mirrors
      ``_stop_autoscaler``).

      Add ``--no-deploy-watcher`` flag on the ``up`` CLI command
      (mirrors ``--no-autoscaler``). The flag is plumbed through
      the same way.

      Tests:
      - Up flow spawns the deploy-watcher when not disabled.
      - The watcher's env contains the deployment ID + the
        operator's AWS profile.
      - ``--no-deploy-watcher`` skips the spawn.
      - ``treadmill-local down`` SIGTERMs the watcher cleanly.
    scope:
      files:
        - tools/local-adapter/treadmill_local/runtime.py
        - tools/local-adapter/treadmill_local/cli.py
        - tools/local-adapter/tests/test_runtime_dev_local.py
        - tools/local-adapter/tests/test_image_build.py
    validation:
      - kind: deterministic
        description: |
          A unit test of ``_up_dev_local`` (with the existing
          stubbed-creds fixtures) confirms
          ``_start_deploy_watcher_dev_local`` is called by default
          and skipped when ``--no-deploy-watcher`` is set.

        script: |
          cd tools/local-adapter && uv run pytest tests/test_image_build.py -q \
            && grep -q "_start_deploy_watcher" treadmill_local/runtime.py
  - id: deploy-watcher-smoke-and-docs
    title: Operator-runbook + manual smoke for deploy-watcher
    workflow: wf-author
    depends_on:
      - task.deploy-watcher-spawn-on-up.pr_merged
    intent: |
      Add a section to the Week-4 plan running log (or ADR-0024
      itself) describing the operator-side smoke:

      1. Run ``treadmill-local up --deployment personal``. Confirm
         the deploy-watcher is reported in the status block.
      2. Make a trivial PR touching ``services/api/treadmill_api/`` —
         e.g., adding a docstring. Merge.
      3. Watch ``.treadmill-local/deploy-watcher.log``: within a
         tick, the watcher should rebuild treadmill-api:dev and
         restart the container.
      4. Verify ``docker ps`` shows treadmill-api with a fresh
         ``Up <seconds> seconds`` status.
      5. Repeat with a PR touching ``workers/agent/`` — watcher
         rebuilds the agent image, no restart.
      6. Repeat with a PR touching ``infra/`` — watcher prints a
         notification, does not run cdk.

      Persist this section so future operators can manually verify
      after redeploys / fresh sessions.
    scope:
      files:
        - docs/plans/2026-05-13-week-4-dev-local-deployment.md
    validation:
      - kind: deterministic
        description: |
          The Week-4 plan's running log contains a section titled
          "Deploy-watcher smoke" (or similar) with the six manual
          steps listed.
        script: |
          grep -q "Deploy-watcher smoke" docs/plans/2026-05-13-week-4-dev-local-deployment.md
```
