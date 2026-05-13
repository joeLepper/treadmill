# ADR-0023: API credentials use long-lived IAM-User keys (not operator SSO)

- **Status:** accepted (2026-05-13)
- **Date:** 2026-05-12 (proposed); 2026-05-13 (accepted)
- **Related:** ADR-0016, ADR-0019 (supersedes §"The API still uses the operator's SSO profile")

## Context

ADR-0019 set up host-side credential injection for dev-local
containers and committed to two distinct credential paths:

> **For the worker**: long-lived IAM-User access keys from
> `worker-aws-credentials`. The local-adapter fetches once per `up`
> and injects as `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`.

> **For the API**: export the operator's SSO session as frozen
> credentials (`access_key` + `secret_key` + `session_token`) via
> `boto3.Session().get_credentials().get_frozen_credentials()`, inject
> as env vars on the API container.

That second path bit us live during the o11y plan-merge smoke
(2026-05-12). The operator's SSO access token has a 1h TTL; the
frozen credentials inherit it. After ~1h the API's boto3 calls fail
with `botocore.exceptions.ClientError: ExpiredToken`. Recovery
required operator action: `aws sso login --profile treadmill-personal`
+ `treadmill-local down && treadmill-local up`. During that recovery,
PR #7's `pr_merged` event was sitting in the inbox waiting; the
trigger handler couldn't fire because the consumer's SQS receive was
returning ExpiredToken.

Operator framing: *"We need to be able to heal frozen sso creds
without an orchestrator getting involved."*

The premise behind ADR-0019's API-uses-SSO choice was operator
convenience:

> "The API runs as a long-lived dev-time container that the operator
> interacts with directly. It uses the operator's SSO via
> `AWS_PROFILE` and the standard env-var-driven credential
> resolution."

But the API's AWS calls aren't on the operator's behalf — they're on
the API's behalf, consuming SQS, publishing SNS, reading Secrets
Manager. There's no "user identity follows operator" requirement.
The convenience reasoning doesn't survive contact with the 1h TTL.

## Decision

### The API gets its own long-lived IAM-User credentials

Mirror ADR-0019's worker-credentials pattern:

- New IAM user provisioned by CDK: `treadmill-<deployment_id>-api`.
- New Secrets Manager secret: `treadmill-<deployment_id>/api-aws-credentials`. JSON shape
  `{"aws_access_key_id": "...", "aws_secret_access_key": "..."}` — same as worker-aws-credentials.
- Operator populates manually post-deploy (one-time setup; CDK creates the user but doesn't generate keys — operator runs `aws iam create-access-key` + `aws secretsmanager put-secret-value`).
- The local-adapter's `_fetch_api_credentials` method mirrors `_fetch_worker_credentials`. Fetches once per `up`; injects as `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` on the API container.
- **Drop the operator-SSO export path entirely.** `_fetch_operator_sso_credentials` retires. No more `AWS_SESSION_TOKEN` on the API container.

### IAM scope: tighter than the operator-SSO defaults

The current SSO frozen-creds inherit whatever the operator's SSO role grants — `AdministratorAccess`-shaped at v0. The new API IAM user gets a narrower policy matching what the API actually does:

- **SQS**: `ReceiveMessage`, `DeleteMessage`, `ChangeMessageVisibility`, `GetQueueAttributes`, `GetQueueUrl` on `treadmill-<deployment_id>-coordination` and `treadmill-<deployment_id>-webhook-inbox`.
- **SQS**: `SendMessage` on `treadmill-<deployment_id>-work.fifo` (the dispatcher publishes to the work queue from the API).
- **SNS**: `Publish` on `treadmill-<deployment_id>-events`.
- **Secrets Manager**: `GetSecretValue` on `treadmill-<deployment_id>/github-webhook-secret` (the webhook poller reads this).

That's it. Four resource scopes, ~7 actions. Compares favorably to `AdministratorAccess` and makes audit + drift detection meaningful.

### The operator's SSO is now used only at `up` time

`treadmill-local up --deployment <id>` still uses the operator's SSO (via `AWS_PROFILE`) to:
- Fetch `worker-aws-credentials` from Secrets Manager.
- Fetch `api-aws-credentials` from Secrets Manager.
- Fetch the GitHub PAT from Secrets Manager.

All three happen in seconds during the `up` flow. The operator's SSO needs to be fresh for that window; after the containers start, neither SSO nor the operator's role identity is needed at runtime.

If the operator's SSO has expired at `up` time, the existing failure mode is clean (SystemExit with a remediation message pointing at `aws sso login`). No silent breakage; no mid-session degradation.

### What this does NOT do

- Doesn't change the worker's credential path (unchanged from ADR-0019).
- Doesn't change the CLI's credential path (still operator's SSO via `AWS_PROFILE` for `treadmill-local init`, etc.).
- Doesn't change the autoscaler subprocess's credentials (still operator SSO via inherited env). The autoscaler can tolerate operator-SSO expiry — see ADR-0018 §"Autoscaler's own AWS credentials." When SSO expires, the autoscaler's SQS polls fail; the failure is operator-visible and operator runs `aws sso login` to recover. That's fine — the autoscaler is a host-side process the operator interacts with.

## Bunkhouse precedent

Bunkhouse runs its API as an ECS Fargate task with an IAM task role. The task role assumption is automatic + AWS-managed — no SSO involved. The Treadmill API in `fully_remote` mode (future ADR) would adopt the same pattern, replacing the IAM-user-key approach with a task role.

This ADR's IAM-user-key approach is the dev-local analog: the API runs on the laptop (not in ECS), so it can't assume a task role; long-lived IAM keys are the closest dev-local equivalent. Same mental model — *the API has its own identity, not the operator's*.

## Trade-offs

- **One more IAM user + one more secret per deployment.** Tracking. Documented in the cold-start runbook + per-deployment YAML. At single-operator scale, this is two manual `put-secret-value` calls during the first deploy + occasional rotation.
- **Operator-side audit becomes accurate.** With AdministratorAccess-via-SSO, every API action was attributable to the operator's identity in CloudTrail. With a dedicated IAM user, API actions are attributable to `treadmill-<id>-api` — cleaner audit, easier to spot anomalies.
- **Manual key rotation cadence.** The operator rotates the API's IAM keys the same way they rotate the worker's — `aws iam create-access-key` + `put-secret-value` + restart the API container + `aws iam delete-access-key`. Recommend quarterly at single-operator scale.
- **One more thing to forget at first-deploy time.** If the operator forgets to populate `api-aws-credentials`, the API will fail to start with a clear "missing API credentials" error. Easy to fix; surfaceable in the runbook.
- **`fully_local` (moto) mode is unaffected** — moto uses dummy creds and `AWS_ENDPOINT_URL` overrides. The API in fully-local doesn't need real AWS credentials at all.

## Alternatives considered

- **Credential refresh broker on the host.** A daemon running on the operator's machine that exposes the AWS container-credential endpoint (port 80 on a metadata-service-like URL) and refreshes the operator's SSO transparently. Boto3 already supports this via `AWS_CONTAINER_CREDENTIALS_FULL_URI`. Solves SSO TTL without adding an IAM user. Rejected: another moving part to supervise + the API would have credentials with the operator's identity (audit ambiguity) + still depends on the operator running `aws sso login` periodically.
- **SSO role chaining.** The operator's SSO grants `sts:AssumeRole` into a long-lived IAM role; the API assumes the role and gets a session token with a 12-hour TTL. Doable; AWS Identity Center supports this. Pushes the TTL from 1h to 12h but doesn't eliminate it — still requires `aws sso login` once a day. Rejected at v0: long-lived IAM user is simpler + has no TTL.
- **Stick with operator SSO + accept 1h cycles.** Operator runs `aws sso login + treadmill-local up` every hour. Reality is the operator doesn't want to babysit. Rejected per operator framing.
- **Wider IAM user policy (`AdministratorAccess`-ish).** Trivially simpler but defeats the "API has a bounded identity" benefit. Rejected.
- **Share `worker-aws-credentials` between worker and API.** One IAM user, both services. Saves one secret + one user. Worse audit (can't tell who did what); worse scope (the worker doesn't need to consume the coordination queue; the API doesn't need to read the GitHub PAT secret). Rejected.

## Open questions

- **Q23.a — Should rotation be automated?** AWS supports automatic Secrets Manager rotation via Lambda. At single-operator scale, manual quarterly rotation is fine; at multi-tenant scale, automation matters. Banked for `TreadmillCloudFull`.
- **Q23.b — What about the autoscaler's credentials?** Per the "What this does NOT do" section, the autoscaler stays on operator SSO. That's fine for now (autoscaler is host-side, operator-supervised). Consider giving the autoscaler its own IAM user in a follow-up if the SSO-expiry friction becomes notable for the autoscaler too.
- **Q23.c — How does this compose with `TreadmillCloudFull`?** When the API runs as an ECS task in AWS, it gets a task role automatically. The IAM-user-key approach retires for production; dev-local keeps using it. Same mental model (API has its own identity), different mechanism. Future ADR.
- **Q23.d — Should `treadmill-local init` provision a default `api-aws-credentials` value?** Currently it doesn't (operator runs `put-secret-value` manually). Could add a `--generate-api-key` flag that runs `aws iam create-access-key` + `put-secret-value` in one shot. Friction reduction; banked.

## Consequences

- **DB / schema**: no change.
- **CDK (`infra/treadmill_infra/constructs/secrets.py` + `iam.py` or sibling)**: add the IAM user resource + its policy + the new secret resource. New CFN outputs: `ApiIamUserArn`, `ApiAwsCredentialsSecretName`.
- **`treadmill-local init`**: pulls the new CFN output into the per-deployment YAML under `secrets.api_aws_credentials_secret_name`.
- **`tools/local-adapter/treadmill_local/runtime.py`**: add `_fetch_api_credentials` (mirrors `_fetch_worker_credentials`). Replace the `_fetch_operator_sso_credentials` call site for the API container env with the new API-creds fetch. Drop the SSO export path from the API env (no `AWS_SESSION_TOKEN`).
- **Tests**: extend `test_runtime_dev_local.py` to assert the API env carries IAM-user-style creds (no `AWS_SESSION_TOKEN`); add a `_fetch_api_credentials` test mirroring `_fetch_worker_credentials`'s.
- **Operator runbook** (in the Week-4 plan + ADR-0016): document the post-deploy step "populate `api-aws-credentials`."
- **ADR-0019 amendment**: §"The API still uses the operator's SSO profile" is superseded by this ADR; add a pointer.
- **Phase 2 self-healing criterion**: the API can run indefinitely without operator intervention. The o11y plan-chain that bit us today can complete end-to-end without a forced cycle.
