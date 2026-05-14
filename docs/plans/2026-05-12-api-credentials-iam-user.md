---
status: active
trigger: ADR-0023 accepted 2026-05-13 (drafted 2026-05-12). Flipped active 2026-05-13; first dispatch produced PR #20 (task 1, merged manually after wf-feedback's action couldn't recover from an empty-diff response to a hallucinated nit). Re-firing 2026-05-13 after fixing the two surfaced gaps (consumer redispatch on pr_merged + wf-feedback empty-diff softening, commit 8a52c17) so tasks 2-4 dispatch cleanly off task 1's merged PR.
parent: docs/plans/2026-05-13-week-4-dev-local-deployment.md
---

# Plan: API credentials → long-lived IAM-User keys per ADR-0023

ADR-0023 commits to giving the API its own IAM user, replacing the
operator-SSO frozen-credentials path that bites every ~1h. This plan
is the implementation.

## Goal

After this plan executes:

1. CDK provisions a new `treadmill-<deployment_id>-api` IAM user with
   a least-privilege inline policy (per ADR-0023 §"IAM scope:
   tighter than the operator-SSO defaults").
2. A new Secrets Manager secret
   `treadmill-<deployment_id>/api-aws-credentials` exists, ready for
   the operator to populate with the IAM user's access key pair.
3. `treadmill-local init` reads the new secret name from CFN output
   into `~/.treadmill/<deployment_id>.yaml` under
   `secrets.api_aws_credentials_secret_name`.
4. `treadmill-local up` fetches the API credentials from Secrets
   Manager (mirror of `_fetch_worker_credentials`); injects as
   `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` on the API
   container.
5. The operator-SSO export path (`_fetch_operator_sso_credentials`)
   retires; no `AWS_SESSION_TOKEN` on the API container.
6. The API runs indefinitely without operator intervention; the
   `ExpiredToken` failure mode observed during the o11y smoke is
   gone.

## Constraints / scope

### In scope

- New IAM user + inline policy in the CDK Secrets construct (or a
  sibling Auth construct).
- New `api-aws-credentials` Secrets Manager secret.
- New CFN outputs: `ApiIamUserArn`, `ApiAwsCredentialsSecretName`.
- `treadmill-local init` schema extension.
- Local-adapter changes: `_fetch_api_credentials` method; API
  container env injection updates; drop the SSO export path for
  the API.
- Tests for all of the above.
- Operator runbook updates documenting the post-deploy
  `put-secret-value` step.

### Out of scope

- Automatic key rotation (Q23.a — banked for `TreadmillCloudFull`).
- Giving the autoscaler subprocess its own IAM user (Q23.b — defer).
- Changes to the worker's credential path (unchanged from ADR-0019).

## Sequence of work

```yaml
# Task 1 (api-iam-user-cdk) landed via PR #20 on 2026-05-13. Trimmed
# from this re-fire so wf-author doesn't hit empty-diff on already-
# merged work. Tasks 2-4 below have had their depends_on adjusted:
# the original task-2 dep (task.api-iam-user-cdk.pr_merged) is
# satisfied in main, so task 2 (now first in the chain) runs with
# no deps. Git history preserves the original 4-task sequence.

sequence_of_work:
  - id: treadmill-local-init-extension
    title: treadmill-local init reads the new CFN output into the YAML
    workflow: wf-author
    intent: |
      Extend ``tools/local-adapter/treadmill_local/deployment_config.py``
      to read ``ApiAwsCredentialsSecretName`` from the deployed
      stack's CFN outputs and write it into the per-deployment YAML
      under ``secrets.api_aws_credentials_secret_name``.

      The YAML schema gains one new key. Existing keys
      (``github_pat_secret_name``,
      ``github_webhook_secret_name``,
      ``worker_aws_credentials_secret_name``) are unchanged.

      Tests in
      ``tools/local-adapter/tests/test_deployment_config.py``: a
      fixture with a synthetic CFN output set + the new key →
      assert the YAML carries the populated value.
    scope:
      files:
        - tools/local-adapter/treadmill_local/deployment_config.py
        - tools/local-adapter/tests/test_deployment_config.py
    validation:
      - kind: deterministic
        description: |
          A deployment config built from a CFN output set including
          ``ApiAwsCredentialsSecretName=treadmill-test/api-aws-credentials``
          serializes with
          ``secrets.api_aws_credentials_secret_name ==
          treadmill-test/api-aws-credentials``.
        script: |
          cd tools/local-adapter && uv run pytest tests/test_deployment_config.py -q \
            && grep -q api_aws_credentials_secret_name treadmill_local/deployment_config.py

  - id: local-adapter-fetch-api-creds
    title: Local-adapter fetches API credentials at up + injects into the API container
    workflow: wf-author
    depends_on:
      - task.treadmill-local-init-extension.pr_merged
    intent: |
      Add ``_fetch_api_credentials`` to
      ``tools/local-adapter/treadmill_local/runtime.py`` — exact
      mirror of ``_fetch_worker_credentials``: read the secret name
      from the deployment config, call Secrets Manager with the
      operator's profile, parse the JSON, return a dict of two env
      vars (``AWS_ACCESS_KEY_ID`` + ``AWS_SECRET_ACCESS_KEY``).

      Wire it into ``_ensure_dev_local_credentials`` so the API
      creds are fetched once per ``up`` and cached on the runtime
      instance. The ``_dev_local_api_env`` builder updates: replace
      the call to ``self._operator_aws_env`` (which currently
      provides SSO-derived ``AWS_ACCESS_KEY_ID`` +
      ``AWS_SECRET_ACCESS_KEY`` + ``AWS_SESSION_TOKEN``) with the
      new ``self._api_aws_env`` (IAM-user-style, two keys only). The
      ``_operator_aws_env`` field + the
      ``_fetch_operator_sso_credentials`` method retire.

      ``AWS_SESSION_TOKEN`` is no longer set on the API container.
      Boto3's env-var credential resolver picks up the two keys; no
      session token is required for IAM-user credentials.

      Error handling: if the API credentials secret has no
      SecretString (operator forgot to populate), raise a clear
      ``RuntimeError`` naming the secret + the operator action
      needed (``aws iam create-access-key ... && aws secretsmanager
      put-secret-value ...``). The runbook documents the same.

      Tests in
      ``tools/local-adapter/tests/test_runtime_dev_local.py``:
        - ``_fetch_api_credentials`` parses a valid JSON payload
          correctly; raises on missing JSON keys.
        - ``_dev_local_api_env`` builder includes
          ``AWS_ACCESS_KEY_ID`` + ``AWS_SECRET_ACCESS_KEY`` from the
          API-user creds; does NOT include ``AWS_SESSION_TOKEN``.
        - The retired ``_fetch_operator_sso_credentials`` is removed;
          the corresponding tests are dropped.
    scope:
      files:
        - tools/local-adapter/treadmill_local/runtime.py
        - tools/local-adapter/tests/test_runtime_dev_local.py
        - tools/local-adapter/tests/test_image_build.py
    validation:
      - kind: deterministic
        description: |
          When the deployment YAML carries
          ``secrets.api_aws_credentials_secret_name``, a stubbed
          Secrets Manager response with valid JSON results in the
          API container env carrying both ``AWS_ACCESS_KEY_ID`` and
          ``AWS_SECRET_ACCESS_KEY`` from the IAM user keys + NOT
          carrying ``AWS_SESSION_TOKEN``.
        script: |
          cd tools/local-adapter && uv run pytest tests/test_runtime_dev_local.py tests/test_image_build.py -q \
            && grep -q "_fetch_api_credentials" treadmill_local/runtime.py \
            && ! grep -q "_fetch_operator_sso_credentials" treadmill_local/runtime.py
      - kind: deterministic
        description: |
          The retired ``_fetch_operator_sso_credentials`` test
          surface is removed; the local-adapter test suite stays
          green after the removal.
        script: |
          cd tools/local-adapter && uv run pytest tests/ -q \
            && ! grep -rq "_fetch_operator_sso_credentials" treadmill_local/ tests/

  - id: operator-runbook-update
    title: Document the post-deploy operator action for API credentials
    workflow: wf-author
    depends_on:
      - task.local-adapter-fetch-api-creds.pr_merged
    intent: |
      Add an operator-runbook section to ADR-0016 (or to the
      Week-4 plan running log; pick whichever fits the existing
      runbook surface) documenting:

        1. After ``cdk deploy`` provisions the new IAM user, run
           ``aws iam create-access-key --user-name
           treadmill-<id>-api --profile treadmill-<id>``. Capture
           the JSON output.
        2. Convert to the ``{aws_access_key_id, aws_secret_access_key}``
           shape (mirror of worker-aws-credentials).
        3. ``aws secretsmanager put-secret-value --secret-id
           treadmill-<id>/api-aws-credentials --secret-string
           '<json>' --profile treadmill-<id>``.
        4. Optional: ``aws iam delete-access-key`` on any prior key
           (rotation case).

      Pair this with the existing worker-aws-credentials runbook
      step so the operator does both in one pass.
    scope:
      files:
        - docs/adrs/0016-dev-local-deployment-topology.md
        - docs/plans/2026-05-13-week-4-dev-local-deployment.md
    validation:
      - kind: deterministic
        description: |
          ADR-0016 or the Week-4 plan running log contains a new
          section titled "Operator runbook: API credentials" (or
          similar) with the four steps above explicitly named.
        script: |
          grep -lqE "(Operator runbook: API credentials|API credentials.*operator)" \
            docs/adrs/0016-dev-local-deployment-topology.md \
            docs/plans/2026-05-13-week-4-dev-local-deployment.md \
            && grep -qE "iam create-access-key" \
              docs/adrs/0016-dev-local-deployment-topology.md \
              docs/plans/2026-05-13-week-4-dev-local-deployment.md \
            && grep -qE "put-secret-value" \
              docs/adrs/0016-dev-local-deployment-topology.md \
              docs/plans/2026-05-13-week-4-dev-local-deployment.md
```
