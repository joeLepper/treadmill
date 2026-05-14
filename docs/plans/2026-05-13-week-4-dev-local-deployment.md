---
status: active
trigger: ADR-0016 + ADR-0017 accepted 2026-05-12
parent: docs/plans/2026-05-08-minimum-runnable-treadmill.md
---

# Plan: Week 4 — dev-local deployment + first real Treadmill GitHub repo

## Trigger

ADR-0016 (dev-local deployment topology) and ADR-0017 (GitHub webhook ingestion via API Gateway + Lambda + SQS) were accepted 2026-05-12. They commit Treadmill to a third deployment mode whose AWS footprint is minimal (~$2/month) and whose compute stays on the laptop. Phase 2 success criteria 2, 4, 5, 8 depend on real GitHub webhook delivery, which this mode enables.

This plan sequences the implementation of dev-local from CDK split through to "Joe's personal Treadmill submits its own first PR against the real Treadmill GitHub repo, in his personal AWS account, with no employer resources touched."

## Goal

Bring Phase 2 to honest closure by exercising the four GitHub-dependent success criteria against a real deployment. Specifically:

1. **Real Treadmill GitHub repo** exists at `github.com/<joe>/treadmill` (private), with a GitHub webhook installed pointing at the dev-local API Gateway URL.
2. **Personal AWS account** hosts the dev-local resources: SNS topics, SQS queues, Secrets Manager secrets, API Gateway, the webhook receiver Lambda.
3. **Joe's laptop / Rainbow runs** the Treadmill API + Postgres + Redis + workers, with boto3 routing to the real AWS resources via the `treadmill-personal` AWS profile.
4. **A trivial Treadmill task** flows end-to-end through the real chain: `treadmill submit` → API → SQS work queue → worker → real `git clone` of the GitHub repo → real Claude Code authoring → `git push` → `gh pr create` → GitHub webhook fires `pull_request:opened` → API Gateway → Lambda → SQS webhook inbox → local poller → signature verify → events table → trigger evaluator → `wf-review` dispatches.

## Success criteria

By close:

1. **CDK stack split** — `TreadmillCloudLite` exists at `infra/treadmill_infra/stacks/cloud_lite.py`; the old `SpikeStack` either renames to it or coexists for the fully-local path. `cdk synth TreadmillCloudLite --context deployment_id=personal` produces the expected ~10-resource CloudFormation template. Assertion tests cover each construct.
2. **DeploymentMode enum** — `Settings.deployment_mode: DeploymentMode` replaces `Settings.local: bool`. All existing call sites updated; tests green.
3. **Lambda webhook receiver** — packaged at `infra/lambdas/webhook_receiver/handler.py`, deployed by `TreadmillCloudLite`, unit-tested with mocked SQS.
4. **Webhook inbox poller** — `services/api/treadmill_api/coordination/webhook_inbox.py` exists; reads envelopes from SQS; verifies signatures; persists Event rows; publishes to SNS. Wired into the API lifespan handler alongside the existing consumer + replay loop.
5. **`treadmill-local init <deployment_id>`** — populates `~/.treadmill/<deployment_id>.yaml` from CloudFormation outputs; idempotent; re-runnable.
6. **`treadmill-local up --deployment <id>`** — dev-local mode boots Postgres + Redis + API as Docker containers with env populated from the yaml file; AWS clients route to real AWS via the named profile.
7. **Worker `REPO_MODE=github`** — re-enabled (was removed in Week-2 closure B.7); clones from `https://github.com/<owner>/<repo>.git` with the PAT from Secrets Manager; pushes branches; opens PRs via `gh pr create`. Tests exercise the github-mode path.
8. **Personal AWS account deployed** — `cdk deploy TreadmillCloudLite --context deployment_id=personal --profile treadmill-personal` succeeds; all CloudFormation outputs land in the yaml file.
9. **GitHub webhook installed** — pointing at the API Gateway URL, signed with the deployed webhook secret, subscribed to `pull_request`, `pull_request_review`, `check_run` events.
10. **End-to-end real-Claude smoke** — `treadmill submit "add a smoke marker"` triggers the full chain; a PR opens on the real Treadmill GitHub repo; `wf-review` fires in response to the webhook; the review event lands in the events table. Manually verified once; documented in the running log.

## Constraints / scope

### In scope

- ADR-0016 + ADR-0017 implementation in code.
- The `TreadmillCloudLite` CDK stack class + the shared constructs under `infra/treadmill_infra/constructs/`.
- The Lambda webhook receiver (~10 lines) + its IAM role + its packaging.
- The webhook-inbox SQS queue + DLQ.
- The `Settings.deployment_mode` migration.
- The `treadmill-local init` CLI subcommand.
- The webhook-inbox poller in the API.
- Re-enabling `REPO_MODE=github` in the worker.
- The personal AWS account deployment + the real Treadmill GitHub repo creation.
- End-to-end smoke against the real chain.

### Out of scope

- **`TreadmillCloudFull`** — production multi-user deployment. Future ADR when evidence of need.
- **GitHub App** — v0 uses PAT. Future ADR.
- **Cross-deployment observability** — each deployment owns its own CloudWatch metrics.
- **The Phase 4 rule engine** — `wf-validate` deterministic check execution stays stubbed per the parent plan.
- **Multi-tier workers** — `compute_tier` field stays unused per ADR-0015.
- **Real GitHub deployment for employer-Treadmill** — this plan covers personal only; the employer deployment is identical mechanics but separate from this plan's success.

### Budget

Researcher estimate: ~50 hours of focused work. Smaller than Week 3's ~70h because (a) this work is mostly mechanical CDK + plumbing, not new architectural shapes; (b) the real-deployment + smoke is the meaty part and gates the whole thing.

## Phased work plan

### Phase A — Settings refactor + CDK construct extraction (~12h)

Foundation. Settings refactor is sequential with everything else (every file that reads `settings.local` needs updating). CDK construct extraction is parallel-able with the Settings work.

**A.1 — `DeploymentMode` enum + `Settings.deployment_mode`** (M, ~4h)
- Files: `services/api/treadmill_api/config.py`; every file that reads `settings.local` (grep first).
- What: define `DeploymentMode = StrEnum("fully_local", "dev_local", "fully_remote")`. Replace `local: bool` with `deployment_mode: DeploymentMode = FULLY_LOCAL`. Update env-var parsing: `TREADMILL_DEPLOYMENT_MODE` with backward-compat for `TREADMILL_LOCAL=true` mapping to `fully_local`.
- Tests: extend `test_config.py` with mode-parsing cases (each enum value; backward-compat from `TREADMILL_LOCAL=true`); update every test that asserted `settings.local == True`.
- Depends on: nothing (start here).

**A.2 — Split `spike.py` constructs + rename resource prefix** (M, ~6h)
- Files: new `infra/treadmill_infra/constructs/{__init__.py, messaging.py, secrets.py}`; rename `infra/treadmill_infra/stacks/spike.py` to `infra/treadmill_infra/stacks/cloud_lite.py`. Extract the SNS/SQS provisioning into `MessagingConstruct`; placeholder `SecretsConstruct` (empty for now, populated in B.2).
- What: the existing `spike.py` becomes `TreadmillCloudLite(stack)` composed of `MessagingConstruct(self, "Messaging", deployment_id=context["deployment_id"])`. Resource names gain the `deployment_id` suffix everywhere — and *also* drop the legacy `treadmill-spike-*` prefix in favor of `treadmill-<deployment_id>-*`. Per ADR-0016's canonical-spellings table, the deployment_id flows in as a snake_case literal (e.g., `personal`) and is PascalCased only for the stack name (`TreadmillPersonalCloudLite`). Mechanical sweep: grep `treadmill-spike-` across `infra/`, `services/`, `tools/`, `tests/`; replace with `treadmill-<deployment_id>-` parameterized. **No fully-local resource names move** — moto-mode resources keep their existing names so existing tests stay green; only the dev-local CDK output changes.
- Tagging assertion: every CDK construct applies a `Tags.of(scope).add("treadmill:deployment_id", deployment_id)` so the operator can run `aws resourcegroupstaggingapi get-resources --tag-filters Key=treadmill:deployment_id,Values=personal` and see exactly what their deployment owns. Cost Explorer also slices on this tag.
- Tests: `infra/tests/test_cloud_lite_stack.py` (rename from `test_spike_stack.py`) — assertion tests for each messaging resource with deployment-suffixed names; **additionally a tagging-assertion test** that every taggable resource in the synthesized template carries `treadmill:deployment_id=<deployment_id>`.
- Depends on: nothing. Can run parallel to A.1.

**A.3 — CDK app dispatch** (S, ~2h)
- Files: `infra/app.py` (the cdk-app entry).
- What: read CDK context flags `deployment_id` + `mode`. Synth `TreadmillCloudLite` when `mode == "dev-local"`. When `mode == "fully-local"` or unset, synth nothing (no-op for `cdk deploy`; fully-local mode doesn't use CDK against AWS, only against moto via the existing local-adapter path). Document.
- Tests: a unit test verifies dispatch logic with each context flag combination.
- Depends on: A.2.

### Phase B — Webhook receiver: CDK + Lambda + IAM (~10h)

**B.1 — `WebhookReceiverConstruct`** (M, ~5h)
- Files: new `infra/treadmill_infra/constructs/webhook_receiver.py`. Composes: API Gateway HTTP API; Lambda function (sourced from `infra/lambdas/webhook_receiver/`); SQS webhook-inbox queue + DLQ; IAM role for the Lambda with `sqs:SendMessage` grant.
- What: per ADR-0017 §"CDK resources." Resource names suffix on `deployment_id`. CloudFormation outputs: `WebhookApiUrl`, `WebhookInboxQueueUrl`, **`WebhookInboxDlqUrl`** (matching ADR-0017's name; this is the queue feeding back into `~/.treadmill/<id>.yaml` under `aws.webhook_inbox_dlq_url`, not the *delivery* DLQ — distinct from the work-queue DLQ that already exists). Lambda env-var injection is explicit: `environment={"WEBHOOK_INBOX_QUEUE_URL": queue.queue_url}` on the `lambda_.Function` constructor; the Lambda's handler reads exactly that name.
- Tagging: this construct also applies the `treadmill:deployment_id` tag (A.2's tagging pattern) to every taggable resource.
- Tests: `infra/tests/test_webhook_receiver_construct.py` — CDK assertion tests for each resource + the Lambda's IAM scope (exactly one queue grant, no other AWS calls); **assertion that `WEBHOOK_INBOX_QUEUE_URL` env var is set on the Lambda**; tagging assertion covers the Lambda + queue + DLQ.

**B.2 — Secrets Manager construct** (S, ~2h)
- Files: extend `infra/treadmill_infra/constructs/secrets.py`. Provisions `treadmill-<deployment_id>/github-webhook-secret` + `treadmill-<deployment_id>/github-pat` as empty secrets (operator populates via `aws secretsmanager put-secret-value` post-deploy).
- What: two `Secret` constructs. CloudFormation outputs: `GithubWebhookSecretName`, `GithubPatSecretName`. The Lambda construct (B.1) reads its secret ARN from the secrets construct's output.
- Tests: assertion tests for both secrets exist + their names follow the deployment-suffix pattern.

**B.3 — Lambda packaging** (S, ~3h)
- Files: new `infra/lambdas/webhook_receiver/handler.py` (~15 lines per ADR-0017, including the `isBase64Encoded` branch); a `requirements.txt` if any deps (stdlib + boto3 which is in the Lambda runtime, so empty). Removal-policy note: when the stack is destroyed, the Lambda function and its log group are removed automatically; no manual cleanup required for B.3.
- What: exactly the code in ADR-0017 §"The Lambda." CDK packages it via `aws_lambda.Code.from_asset("infra/lambdas/webhook_receiver")`.
- Tests: `infra/lambdas/webhook_receiver/tests/test_handler.py` — unit test with mocked SQS client; assert envelope shape (`{headers, body}` with the three preserved headers); **assert `isBase64Encoded=True` path decodes correctly**; assert envelope JSON is well-formed UTF-8.

**B.4 — `WebhookInboxEnvelope` Pydantic model** (S, ~1h)
- Files: new `services/api/treadmill_api/webhooks/inbox_envelope.py` with the Pydantic model per ADR-0017 §"The Pydantic boundary type"; unit test in `tests/test_inbox_envelope_unit.py`.
- What: the shared boundary type both the Lambda's envelope output and the poller's input agree on. `extra="forbid"` so unknown fields fail loud. Placed in `services/api/treadmill_api/webhooks/` alongside `signatures.py` and `normalize.py`.
- Tests: positive case (valid envelope round-trips); negative cases (missing `headers`, missing `body`, extra field rejected by `extra=forbid`).
- Depends on: nothing. Can run alongside B.3.

**B.5 — CloudWatch billing alarm** (S, ~1h)
- Files: extend `infra/treadmill_infra/constructs/observability.py` (new module).
- What: per-deployment CloudWatch billing alarm at $10/month (configurable via CDK context `billing_alarm_threshold`). Topic: SNS for alarm notifications (subscribed to operator email post-deploy via manual `aws sns subscribe`; v0 doesn't auto-subscribe). Catches the failure mode where a misconfigured Lambda loop or runaway SQS retention spikes cost — operator gets paged before the credit-card statement.
- Tests: assertion test that the alarm exists with the expected threshold + that the `Currency=USD` dimension is set.
- Depends on: nothing. Can fire parallel to B.1/B.2/B.3/B.4.

### Phase C — Local-side wiring: poller + init command + adapter (~14h)

**C.1 — Webhook-inbox poller** (M, ~6h)
- Files: new `services/api/treadmill_api/coordination/webhook_inbox.py`. Mirror the existing `coordination/consumer.py` shape — `WebhookInboxPoller` class with `start()` / `stop()` / `_run()` matching the consumer pattern.
- What: long-poll the inbox queue; validate envelope via `WebhookInboxEnvelope.model_validate_json` (from B.4); fetch webhook secret from Secrets Manager (cached at startup); call `verify_github_signature` from existing `webhooks/signatures.py`; on success, derive `event_id = uuid.uuid5(NAMESPACE_OID, x-github-delivery)`, delegate to existing `webhooks/normalize.py`, persist Event row with `ON CONFLICT (id) DO NOTHING`, publish to SNS. On failure: log + delete (poison-safe). **Signature-failure logging never includes the body or repo/PR fields** — log only SQS message ID, derived event_id, and "signature failed" reason (per ADR-0017).
- Inherits from `coordination/consumer.py` (Phase-3 closure fixes — enumerated so the implementation doesn't drift):
  - Exponential backoff `1, 2, 4, 8, 16, 30` seconds on SQS poll failure (see `consumer.py` `_backoff_seconds`).
  - `_failures_before_error_log` escalation (warn-then-error after N consecutive failures).
  - `_health_status` reported via a `WebhookInboxProbe` sibling to `CoordinationProbe` (FastAPI `/healthz` includes it).
  - Malformed-SQS-message poison-safe deletion (envelope validation failure or signature-fail → delete, not retry).
- Tests: unit tests in `tests/test_webhook_inbox_unit.py` (handler with stub clients; envelope-validation failure path; signature-fail path; happy path); integration test in `tests/test_integration_webhook_inbox.py` against live moto (and later, real AWS in E.3 smoke); **assert `event_id` is deterministic across two arrivals of the same `x-github-delivery`** (idempotency contract).

**C.2 — Wire poller into API lifespan** (S, ~2h)
- Files: `services/api/treadmill_api/app.py` (add the poller to lifespan startup alongside the existing consumer + replay loop). New env var: `WEBHOOK_INBOX_QUEUE_URL`. `config.py` adds the setting.
- What: lifespan starts the poller when `deployment_mode in {DEV_LOCAL, FULLY_REMOTE}` AND `webhook_inbox_queue_url` is set. Stops cleanly on shutdown.
- Tests: extend `test_health.py` if there's a poller health probe; otherwise smoke via the integration test in C.1.

**C.3 — Disable HTTP webhook route in dev-local mode** (S, ~2h)
- Files: `services/api/treadmill_api/routers/webhooks.py`.
- What: the existing `POST /api/v1/webhooks/github` route gains a guard: when `settings.deployment_mode != FULLY_LOCAL`, return 503 with `detail="webhook ingestion is via the AWS-side path in this mode"`. Fully-local mode keeps the route for fast iteration.
- Tests: extend `test_integration_webhooks.py` with two mode-gated cases (fully-local: 202; dev-local: 503).

**C.4 — `treadmill-local init <deployment_id>`** (M, ~4h)
- Files: extend `tools/local-adapter/treadmill_local/cli.py`; new helper module `tools/local-adapter/treadmill_local/deployment_config.py`.
- What: subcommand reads `aws cloudformation describe-stacks --stack-name Treadmill<DeploymentId>CloudLite --query Outputs` via boto3; writes `~/.treadmill/<deployment_id>.yaml` per ADR-0016 schema. Idempotent. Validates: all expected outputs present; secret names refer to existing Secrets Manager entries (warns if not).
- Tests: unit test with mocked boto3 CloudFormation client; assert the written YAML matches the expected schema.

### Phase D — Worker GitHub mode + dev-local adapter mode (~10h)

**D.1 — Re-enable `REPO_MODE=github`** (M, ~5h)
- Files: `workers/agent/treadmill_agent/git.py` (re-introduce the `mode=="github"` branch in `clone` + `open_pr`); `workers/agent/treadmill_agent/config.py` (GITHUB_PAT setting); `workers/agent/Dockerfile` (no change — `gh` already installed); tests in `workers/agent/tests/test_git.py`.
- What: github-mode clone uses **`gh auth login --with-token`** at worker startup (per ADR-0016 Q16.d) — *not* the token-in-URL form. Token-in-URL leaks the secret into `git config`, `git log`, `~/.bash_history`, and any subprocess `proc/<pid>/cmdline` snapshot; `gh auth login --with-token` stores the credential in `gh`'s keyring and propagates it into the environment via `gh auth setup-git`. Clone shells to `git clone https://github.com/<owner>/<repo>.git` (no auth token in the URL — `gh auth setup-git` installed a credential helper). `open_pr` shells to `gh pr create` (no `GH_TOKEN` env required — `gh` uses its keyring). The PAT itself is never written to the worker filesystem outside `gh`'s keyring.
- Tests: extend `test_git.py` with github-mode tests against a `gh` stub binary (like the existing `claude_code.py` test stub pattern); **assert the PAT never appears in any argv or env recorded by the stub** (leak-prevention regression test). Real-GitHub smoke is in E.3.

**D.2 — Dev-local mode in local-adapter** (M, ~3h)
- Files: `tools/local-adapter/treadmill_local/runtime.py` (extend `_ensure_provisioned` + container env wiring); `tools/local-adapter/treadmill_local/cli.py` (add `--deployment` flag).
- What: when `--deployment <id>` is set, read `~/.treadmill/<id>.yaml`; skip moto; start Postgres + Redis + API containers with env from the yaml (real ARNs, `AWS_PROFILE`, no `AWS_ENDPOINT_URL`). Worker container also gets the env.
- Tests: extend `test_runtime.py` with a dev-local fixture (mocks the yaml file + skips moto invocation); assert correct env injection.

**D.3 — Worker reads PAT from Secrets Manager at startup** (S, ~2h)
- Files: `workers/agent/treadmill_agent/config.py`; `workers/agent/treadmill_agent/__main__.py`.
- What: at worker startup, if `GITHUB_PAT_SECRET_NAME` env is set (dev-local mode — naming reconciled with ADR-0016's YAML `secrets.github_pat_secret_name`, **not** `_SECRET_ARN`; the name resolves to ARN via Secrets Manager's name-or-ARN lookup), fetch the PAT value from Secrets Manager via boto3, then pipe it into `gh auth login --with-token` (per D.1's auth method) and run `gh auth setup-git`. The PAT value is held in memory only for the duration of those two commands; never written to disk by worker code. Fail-fast at startup if the secret can't be fetched. Worker auth credentials for boto3 itself come from `WORKER_AWS_CREDENTIALS_SECRET_NAME` (ADR-0016 Q16.c — long-lived IAM-User keys) or from the standard AWS profile/role chain in fully-remote mode.
- Tests: unit test with mocked Secrets Manager client; **assert that after startup, no environment variable or filesystem path contains the PAT plaintext** (other than what `gh` itself stores in its keyring).

### Phase E — Real deployment + end-to-end smoke (~6h)

**E.1 — Cold-start deployment to personal AWS** (M, ~3h)
- Manual: per the cold-start walkthrough in the research report.
  1. `aws configure sso --profile treadmill-personal` (or long-lived keys).
  2. `cdk bootstrap aws://<account>/us-east-1 --profile treadmill-personal`.
  3. `cdk deploy TreadmillCloudLite --context deployment_id=personal --profile treadmill-personal`.
  4. `aws secretsmanager put-secret-value --secret-id treadmill-personal/github-pat --secret-string "<PAT>"`.
  5. `aws secretsmanager put-secret-value --secret-id treadmill-personal/github-webhook-secret --secret-string "$(openssl rand -hex 32)"`.
  6. `treadmill-local init personal --stack-name TreadmillPersonalCloudLite --profile treadmill-personal`.
- Documented in the closure plan running log; manual but reproducible.

**E.2 — Create real Treadmill GitHub repo + install webhook** (S, ~1h)
- Manual: `gh repo create treadmill --private`. Install webhook pointing at the API Gateway URL with the webhook secret:

  ```
  gh api -X POST /repos/<joe>/treadmill/hooks \
    -f name=web \
    -f config[url]=<WebhookApiUrl>/webhook/github \
    -f config[content_type]=json \
    -f config[secret]=<webhook-secret> \
    -f 'events[]=pull_request' \
    -f 'events[]=pull_request_review' \
    -f 'events[]=check_run'
  ```

  Verify via `gh api /repos/<joe>/treadmill/hooks/<id>/deliveries` — the install ping should land 202.

- Validate the ping: `treadmill-local up --deployment personal`; check the events table for the github.ping event (or simply that the poller logged the receipt).

**E.3 — End-to-end smoke** (M, ~2h)
- Manual: `treadmill submit "trivial change" --repo <joe>/treadmill`. Watch:
  1. API dispatches → SQS work queue claim.
  2. Worker picks up the claim, clones the real repo via PAT, runs Claude Code, commits, pushes branch.
  3. `gh pr create` opens a real PR.
  4. GitHub fires `pull_request:opened` webhook → API Gateway → Lambda → SQS webhook inbox.
  5. Poller receives, verifies signature, persists event, publishes to SNS.
  6. Trigger evaluator dispatches `wf-review`.
  7. `wf-review` worker runs, posts comments.
- Document the smoke result in the closure plan running log. Phase 2 success criteria 2, 4, 5, 8 satisfied if all of the above lands.

### Explicitly deferred

- **`TreadmillCloudFull`** — production multi-user. Future ADR.
- **Employer-Treadmill deployment** — same mechanics, separate account. Not Phase 4's scope.
- **GitHub App migration** — future ADR.
- **Real GitHub webhook delivery in integration tests** — v0 stays manual; CI doesn't fire real GitHub webhooks.

## Phased agent partitions

When firing parallel agents:

- **Phase A — 3 agents (parallel, file-isolated):**
  - Agent 1: A.1 — `DeploymentMode` enum + Settings refactor (touches `config.py` + every test reading `settings.local`).
  - Agent 2: A.2 — CDK construct extraction (touches `infra/treadmill_infra/`).
  - Agent 3: A.3 — CDK app dispatch (touches `infra/app.py`); depends on A.2 but can start drafting in parallel.

- **Phase B — 5 agents (parallel after A.2):**
  - Agent 1: B.1 — `WebhookReceiverConstruct`.
  - Agent 2: B.2 — Secrets Manager construct.
  - Agent 3: B.3 — Lambda packaging + unit test.
  - Agent 4: B.4 — `WebhookInboxEnvelope` Pydantic model. (Touches `services/api/treadmill_api/webhooks/`; file-isolated from B.1-B.3.)
  - Agent 5: B.5 — CloudWatch billing alarm construct. (Touches `infra/treadmill_infra/constructs/observability.py`; file-isolated from B.1-B.4.)

- **Phase C — 3 agents (parallel after A.1 + B.1 + B.4):**
  - Agent 1: C.1 + C.2 — webhook-inbox poller + lifespan wiring. (Blocked by B.4 — the poller validates via `WebhookInboxEnvelope`.)
  - Agent 2: C.3 — disable HTTP route in dev-local mode.
  - Agent 3: C.4 — `treadmill-local init` subcommand.

- **Phase D — 3 agents (parallel after C):**
  - Agent 1: D.1 — re-enable `REPO_MODE=github`.
  - Agent 2: D.2 — dev-local mode in local-adapter.
  - Agent 3: D.3 — worker reads PAT from Secrets Manager.

- **Phase E — manual** (Joe + orchestrator together, one machine, one substrate).

## Cross-ADR consistency points

- **ADR-0016's CDK stack split** (TreadmillCloudLite vs TreadmillCloudFull) ↔ Phase A.2's construct extraction. The constructs must be reusable by both stack classes; Phase A.2 sets the pattern.
- **ADR-0016's `~/.treadmill/<deployment>.yaml` schema** ↔ Phase C.4's `treadmill-local init` output. The YAML schema is the contract; the init command produces exactly that shape.
- **ADR-0017's `{headers, body}` envelope** ↔ Phase B.3's Lambda + Phase B.4's Pydantic model + Phase C.1's poller. All three cite the ADR by section; the Pydantic model is the shared contract.
- **ADR-0017's `WEBHOOK_INBOX_QUEUE_URL` Lambda env var** ↔ Phase B.1's `environment=` kwarg ↔ Phase B.3's `os.environ[...]` lookup. Single canonical spelling; assertion test in B.1 catches drift.
- **ADR-0016's `treadmill:deployment_id` tag** ↔ Phase A.2's tagging discipline ↔ Phase B.1/B.5 inheritance. Every CDK construct picks up the tag from the parent scope; the assertion test in A.2 is the regression net.
- **ADR-0016's YAML `secrets.github_pat_secret_name`** ↔ Phase D.3's `GITHUB_PAT_SECRET_NAME` env var. Same name, two surfaces (YAML key vs env var); local-adapter is the bridge.
- **ADR-0016 Q16.d's `gh auth login --with-token`** ↔ Phase D.1's worker auth path. The auth method is load-bearing for credential hygiene.
- **ADR-0017's signature-verification on dequeue** ↔ Phase C.1's poller code. The poller calls existing `webhooks/signatures.py:verify_github_signature` — no new crypto code.
- **The existing `webhooks/normalize.py`** ↔ Phase C.1's poller. The poller's "valid signature → process" path delegates to normalize.py exactly as the HTTP route does.

## Risks / unknowns

- **CDK bootstrap on a fresh personal account** — Joe may not have AWS credentials set up yet. Mitigation: the cold-start walkthrough documents the exact steps; he can do it once before Phase E.
- **PAT scopes** — token needs `repo` + `admin:repo_hook` minimum. If we discover it needs more, document in the running log + retry.
- **Lambda cold-start latency** — first webhook after the deployment may be slow (~200ms). Webhook delivery isn't time-critical; GitHub gives ~10s before retry.
- **API Gateway URL stability** — the auto-generated URL is stable for the deployment's lifetime; if the API Gateway is destroyed + recreated, the URL changes and the GitHub webhook must be reinstalled. Document the procedure.
- **The webhook receiver Lambda has minimal logic but must not throw** — a throwing Lambda returns 5xx to API Gateway; GitHub retries; messages still arrive but with delay. Mitigation: the Lambda is ~10 lines, dead simple, with defensive `.get()` access to event fields.
- **Multi-deployment confusion** — Joe runs personal-treadmill commands against employer-treadmill resources by accident. Mitigation: deployment-suffixed resource names everywhere; CLI prints the deployment ID at every command's start; AWS profile must match the deployment.

## Operator runbooks (drafted now, validated in Phase E)

### Stack deletion + redeploy

When the operator needs to tear down a dev-local deployment cleanly:

```bash
# 1. Empty Secrets Manager values *before* stack deletion (otherwise the
#    Secret resource hits its 7-30 day deletion-protection window).
aws secretsmanager delete-secret \
  --secret-id treadmill-personal/github-webhook-secret \
  --force-delete-without-recovery \
  --profile treadmill-personal
aws secretsmanager delete-secret \
  --secret-id treadmill-personal/github-pat \
  --force-delete-without-recovery \
  --profile treadmill-personal
aws secretsmanager delete-secret \
  --secret-id treadmill-personal/worker-aws-credentials \
  --force-delete-without-recovery \
  --profile treadmill-personal

# 2. Drain the webhook inbox queue + DLQ (optional — destroying the queue
#    discards messages anyway, but if the operator wants to inspect them
#    first this is the moment).
aws sqs purge-queue \
  --queue-url "$(yq .aws.webhook_inbox_queue_url ~/.treadmill/personal.yaml)" \
  --profile treadmill-personal

# 3. Destroy the stack.
cdk destroy TreadmillPersonalCloudLite --profile treadmill-personal

# 4. Remove the local YAML config + GitHub webhook installation.
rm ~/.treadmill/personal.yaml
gh api -X DELETE /repos/<joe>/treadmill/hooks/<id>
```

Redeploy after destroy is just Phase E.1 + E.2 verbatim. The **API Gateway URL changes** on redeploy (it's auto-generated per deployment), so the GitHub webhook URL in step 4 must be updated post-redeploy via `gh api -X PATCH /repos/<joe>/treadmill/hooks/<id> -f config[url]=<new-url>`. ADR-0016 §"What's deferred" notes this as a known limitation of v0 (resolved later by a custom domain).

The `removalPolicy=DESTROY` + `forceDeletion=True` props on the `Secret` constructs in B.2 are load-bearing here — without them, step 3 (`cdk destroy`) hangs on the Secrets Manager 30-day deletion-protection window. Confirm those props are set during code review of B.2.

### Operator runbook: API credentials (worker + API post-deploy setup)

After `cdk deploy TreadmillCloudLite` provisions the IAM users and empty secrets, the operator must populate the AWS credentials for both the worker and the API. This is a single multi-step operation; do both in sequence:

#### Step 1: Create and store worker AWS credentials

The CDK has provisioned an IAM user `treadmill-<deployment_id>-worker` with scoped policy. Create its access key and store it in Secrets Manager:

```bash
# 1. Generate access key for the worker IAM user.
aws iam create-access-key \
  --user-name treadmill-personal-worker \
  --profile treadmill-personal

# 2. The output includes AccessKeyId + SecretAccessKey. Capture the JSON output,
#    then extract and re-shape to {aws_access_key_id, aws_secret_access_key}.
#    Store in the Secrets Manager secret:
aws secretsmanager put-secret-value \
  --secret-id treadmill-personal/worker-aws-credentials \
  --secret-string '{"aws_access_key_id": "AKIA...", "aws_secret_access_key": "..."}' \
  --profile treadmill-personal
```

#### Step 2: Create and store API AWS credentials

The CDK has provisioned an IAM user `treadmill-<deployment_id>-api` with scoped policy (read only on Secrets Manager, not SNS/SQS). Create its access key and store it in Secrets Manager:

```bash
# 1. Generate access key for the API IAM user.
aws iam create-access-key \
  --user-name treadmill-personal-api \
  --profile treadmill-personal

# 2. The output includes AccessKeyId + SecretAccessKey. Capture the JSON output,
#    then extract and re-shape to {aws_access_key_id, aws_secret_access_key}.
#    Store in the Secrets Manager secret:
aws secretsmanager put-secret-value \
  --secret-id treadmill-personal/api-aws-credentials \
  --secret-string '{"aws_access_key_id": "AKIA...", "aws_secret_access_key": "..."}' \
  --profile treadmill-personal
```

#### Step 3 (optional): Rotate old API key if re-deploying

On subsequent deploys, if the API user already has an access key from a prior deployment, delete it to enforce one-key-per-user:

```bash
# List existing keys for the API user (should show old + new if this is a redeploy).
aws iam list-access-keys \
  --user-name treadmill-personal-api \
  --profile treadmill-personal

# Delete the old AccessKeyId (keep the one you just created).
aws iam delete-access-key \
  --user-name treadmill-personal-api \
  --access-key-id AKIA... \
  --profile treadmill-personal
```

### Webhook secret rotation

```bash
# Generate + put new secret. Existing value is overwritten atomically.
NEW_SECRET=$(openssl rand -hex 32)
aws secretsmanager put-secret-value \
  --secret-id treadmill-personal/github-webhook-secret \
  --secret-string "$NEW_SECRET" \
  --profile treadmill-personal

# Update GitHub's webhook to the new secret.
gh api -X PATCH /repos/<joe>/treadmill/hooks/<id> \
  -f config[secret]="$NEW_SECRET"

# Restart the API so the poller re-reads the secret (it's cached at startup).
treadmill-local up --deployment personal --restart-api
```

GitHub provides a way to verify both old and new secrets simultaneously during rotation, but at single-operator scale the brief outage from an API restart is acceptable. If concurrent old+new verification becomes a real need, switch from cache-at-startup to refresh-on-cache-miss (Q17.a in ADR-0017).

### PAT rotation

```bash
# Generate a new fine-grained PAT in GitHub UI with: repo + admin:repo_hook.
aws secretsmanager put-secret-value \
  --secret-id treadmill-personal/github-pat \
  --secret-string "<NEW_PAT>" \
  --profile treadmill-personal

# Restart the worker so it re-runs `gh auth login --with-token` with the new value.
treadmill-local up --deployment personal --restart-worker

# Revoke the old PAT in GitHub UI.
```

The order matters: put the new value, restart the worker (which re-fetches and re-auths), *then* revoke the old. A failed restart leaves the worker running on the old credential until the next deployment cycle.

### "The DLQ has messages — what now?"

```bash
DLQ_URL=$(yq .aws.webhook_inbox_dlq_url ~/.treadmill/personal.yaml)

# Inspect a sample message (the envelope JSON).
aws sqs receive-message \
  --queue-url "$DLQ_URL" \
  --max-number-of-messages 10 \
  --visibility-timeout 60 \
  --profile treadmill-personal

# Typical causes:
#   - signature verification failed 5x (probably wrong secret in Secrets Manager)
#   - envelope JSON malformed (probably a Lambda code regression)
#   - poller DB write failed 5x (database down or schema mismatch)
# Diagnose, fix root cause, then either re-drive (copy message body to main queue)
# or purge:
aws sqs purge-queue --queue-url "$DLQ_URL" --profile treadmill-personal
```

## Decisions captured during execution

(filled in as work progresses)

## Running log

- **2026-05-12** Plan authored. ADR-0016 + ADR-0017 accepted. Bunkhouse cloud-native deep-dive completed (researcher's report quotes lines from `bunkhouse-stack.ts` + `messaging.ts` + `compute.ts` + `loadbalancer.ts` + `webhooks.py`). Verified: bunkhouse currently runs API + DB + cache in AWS (~$150-250/mo) and uses synchronous HMAC verification in the API HTTP handler.
- **2026-05-12** *Correction.* The initial researcher framed bunkhouse's APIGW→SQS pattern as an "abandoned prototype." Operator pushback (`"you're wrong"`) prompted git archaeology; commit `d357e47e` on 2026-01-29 ("Add AWS SQS infrastructure via CDK") shipped the pattern as a real, deployed shape. Bunkhouse retired it not because the pattern was wrong but because bunkhouse's topology changed (API moved into AWS, making buffered-webhook unnecessary). Treadmill's dev-local matches bunkhouse's *Jan 29 topology*, so we crib that shape and add the Lambda wrapper for header preservation. Learning captured at `docs/learnings/2026-05-12-precedent-history-not-just-current-state.md`; ADR-0017's bunkhouse-precedent section rewritten to reflect the corrected history. Phase A ready to fire.
- **2026-05-12** Adversarial review fired against ADRs 0016 + 0017 + this plan. 12 P0 + multiple P1 findings landed; operator authorized "just do it." All findings applied in-line — see ADR-0016/0017 history + this plan's edits below the running log.
- **2026-05-13** **Phase A closed.** Three sub-agents fired in parallel (A.1 + A.2), then A.3 sequentially.
  - **A.1** (Settings refactor): `DeploymentMode` enum with `fully_local`/`dev_local`/`fully_remote` literals; `Settings.local: bool` retired in favor of `Settings.deployment_mode: DeploymentMode`; backward-compat (`TREADMILL_LOCAL=true` → `FULLY_LOCAL`, `=false` → `FULLY_REMOTE`); new `aws_account_id` + `webhook_inbox_queue_url` fields; `is_fully_local` helper property. Only call-site touched was `routers/plans.py`'s dev-fast-path gate. Tests: 207 passed (17 new in `test_config.py`).
  - **A.2** (CDK construct extraction): new `constructs/{messaging,secrets}.py`; `SpikeStack` → `TreadmillCloudLite` with `deployment_id` kwarg (regex-validated against `^[a-z][a-z0-9]{0,29}$`); CFN stack name computed as `Treadmill{PascalCase(deployment_id)}CloudLite`; `cdk.Tags.of(self).add("treadmill:deployment_id", ...)` applied; resource names swept from `treadmill-spike-*` → `treadmill-<deployment_id>-*`. Tests: 30 passed (deployment-suffixed names; tagging assertion; 6 valid + 9 invalid deployment_id cases). **Scope call:** dropped the legacy ECS/VPC/S3/Postgres/Redis/API-Fargate block from `TreadmillCloudLite` — ADR-0016 commits CloudLite to AWS-side resources only (compute is local); Fargate scaffolding belongs to a future `TreadmillCloudFull` ADR.
  - **A.3** (CDK app dispatch): `app.py` factored into a testable `synthesize(app, context) -> list[Stack]`; reads `mode` + `deployment_id` from CDK context; `dev_local` instantiates `TreadmillCloudLite`, other modes no-op with stderr messages; unknown mode raises with allowed-modes message. Verified via `cdk ls` + `cdk synth` against the real CDK CLI. Tests: 12 new dispatch tests (42 total in `infra/tests/`).
  - **Finding banked for Phase E.1**: `cdk.json` specifies `"app": "python -m treadmill_infra.app"`, which fails on a fresh shell where `python` (not `python3`) isn't on PATH. Cold-start walkthrough should either change `cdk.json` to `"uv run python ..."` or document the `uv run cdk` invocation pattern. Not Phase A's scope; surfaced now so Phase E doesn't rediscover.
- **2026-05-13** **Phase B closed.** Five sub-agents fired in parallel (B.1-B.5), then a composition pass wired the new constructs into `TreadmillCloudLite`.
  - **B.1** (WebhookReceiverConstruct): API Gateway HTTP API at `treadmill-<id>-webhook-api` with `POST /webhook/github` route; Lambda `treadmill-<id>-webhook-receiver` with `WEBHOOK_INBOX_QUEUE_URL` env var; SQS inbox + DLQ with 60s visibility timeout, 14-day retention, `maxReceiveCount=5`; CFN outputs `WebhookApiUrl`/`WebhookInboxQueueUrl`/`WebhookInboxDlqUrl`. 14 new tests including IAM scope assertion (Lambda gets exactly `sqs:SendMessage`+`GetQueueUrl`+`GetQueueAttributes` on one queue, no other AWS access). **Quirk**: CDK's `grant_send_messages` includes the two `Get*` actions on top of `SendMessage` (boto3 helper convention); test was relaxed to allow that canonical set rather than literal `SendMessage`-only.
  - **B.2** (SecretsConstruct populated): three secrets at `treadmill-<id>/{github-webhook-secret,github-pat,worker-aws-credentials}`, all empty containers (operator populates post-deploy). `removal_policy=DESTROY` + CFN-escape-hatch `ForceDeleteWithoutRecovery=True` so the stack-deletion runbook works. CFN outputs use deterministic Python literals (not CDK tokens) so `treadmill-local init` can parse them. 12 new tests.
  - **B.3** (Lambda handler): `infra/lambdas/webhook_receiver/handler.py` per ADR-0017 §"The Lambda" verbatim, including the `isBase64Encoded` decode path. 8 unit tests + a `conftest.py` that seeds `WEBHOOK_INBOX_QUEUE_URL` + `AWS_DEFAULT_REGION` at import time (the module-level `boto3.client("sqs")` requires `AWS_REGION` to be set). **Asset-hygiene follow-up banked**: the `tests/` dir currently ships with the deployed Lambda asset; harmless but a one-line `Code.from_asset(exclude=["tests/**"])` would slim the bundle. Surface in code review.
  - **B.4** (WebhookInboxEnvelope): Pydantic v2 model at `services/api/treadmill_api/webhooks/inbox_envelope.py` with `extra="forbid"`. 8 unit tests covering round-trip, missing fields, extra fields, wrong types, empty values, realistic GitHub payloads. JSON-mode validation rejects integer→str coercion for `body`; locked in via test.
  - **B.5** (ObservabilityConstruct): CloudWatch billing alarm against `AWS/Billing` `EstimatedCharges` (us-east-1-only namespace, documented in module docstring); $10 default threshold, 6h period, `GreaterThanThreshold`. SNS topic `treadmill-<id>-billing-alarms` for alarm fan-out; operator subscribes email post-deploy (manual, intentional — no email-confirmation friction at `cdk deploy` time). 16 new tests.
  - **Composition pass**: `constructs/__init__.py` re-exports all four constructs; `TreadmillCloudLite` composes `messaging` + `secrets` + `webhook_receiver` + `observability`. Updated the resource-count assertion to reflect the composed shape: 6 SQS queues, 2 SNS topics, 3 secrets, 1 Lambda, 1 alarm. End-to-end `cdk synth TreadmillPersonalCloudLite --context mode=dev_local --context deployment_id=personal` produces a valid template.
  - Test totals: 84 infra tests + 8 envelope tests + 8 Lambda tests = **100 tests green for Phase B work**.
  - **Flake observed once but not reproducing**: `test_taggable_resources_carry_deployment_id_tag` in `test_webhook_receiver_construct.py` flaked once during the parallel run (TypeError on a CDK token), passed cleanly when re-run. Likely a synthesis-order race when many constructs synth concurrently in test setup. Monitor; if it recurs, investigate.
- **2026-05-13** **Phase C closed.** Three sub-agents fired in parallel (C.1+C.2 / C.3 / C.4).
  - **C.1+C.2** (`coordination/webhook_inbox.py` + lifespan wiring): `WebhookInboxPoller` mirrors `consumer.py`'s Phase-3-closure-fixed shape (exponential backoff `[1,2,4,8,16,30]`s, `_FAILURES_BEFORE_ERROR_LOG=5`, health probe, poison-safe deletion). Validates `WebhookInboxEnvelope`, derives `event_id = uuid.uuid5(NAMESPACE_OID, x-github-delivery)`, calls existing `verify_github_signature` + `normalize.py`, upserts Event row with `ON CONFLICT DO NOTHING`, publishes through `SNSEventPublisher`. Signature-failure log scrubbing is enforced — tests assert that body content / repo names / PR fields never appear in any LogRecord on a signature failure. New `WebhookInboxProbe` is sibling to `CoordinationProbe`; `/healthz` flips on staleness. Settings: `github_webhook_secret_name: str | None` added; lifespan starts the poller iff `deployment_mode in {dev_local, fully_remote} AND webhook_inbox_queue_url AND github_webhook_secret_name`. 27 unit tests + 3 `TREADMILL_INTEGRATION=1`-gated moto+Postgres integration tests. **Banked for code review**: `_extract_commit_sha` is duplicated between `routers/webhooks.py` (HTTP route) and `coordination/webhook_inbox.py` (poller); both ingress paths implement ADR-0014's commit_sha extraction. Single shared helper is the obvious lift; defer until both paths settle.
  - **C.3** (HTTP route gating): `routers/webhooks.py` now declares `dependencies=[Depends(require_fully_local_mode)]` on the `POST /api/v1/webhooks/github` route. Returns 503 with `detail="webhook ingestion is via the AWS-side path in this mode; see ADR-0017"` in `dev_local` + `fully_remote`. Gate fires before signature verification (FastAPI route-level dependency runs before body read). Refactored `settings` to use `Depends(get_settings)` matching `routers/plans.py`'s pattern (small style improvement; eliminates `lru_cache` test bleed). 6 new tests.
  - **C.4** (`treadmill-local init`): new Typer subcommand. Helper module `tools/local-adapter/treadmill_local/deployment_config.py` exposes `read_stack_outputs` + `build_deployment_config` + `write_deployment_yaml`. Suffix-match on CDK's hash-suffixed output keys so the contract survives logical-id renames. Writes YAML to `~/.treadmill/<deployment_id>.yaml` per ADR-0016. `sts.get_caller_identity` preflight populates `aws_account_id` (quoted as a string — Bash leading-zero defense). Idempotent (overwrites on re-run, prints notice). 18 new tests including end-to-end Typer `CliRunner` invocation against mocked boto3. pyyaml chosen over ruamel (already a dep in `services/api`; comment preservation isn't needed since `init` regenerates from scratch).
  - **A.2 gap discovered + fixed in C.4 scope**: Phase A.2's `messaging.py` shipped **without any `CfnOutput`s** — neither `EventsTopicArn` nor `EventsQueueUrl` nor `WorkQueueUrl` existed, despite the plan + running log treating them as the contract. `treadmill-local init` would have crashed with `KeyError` against a real stack. C.4 added the three outputs (3 lines each, no behavioral change). Lesson: A.2's "84 passed" was true but the test suite didn't assert CFN-output presence — a class of regression the existing assertion suite doesn't catch. Add output-presence assertions to `test_cloud_lite_stack.py` in a future cleanup phase, or pull them into each construct's own test file (B.1/B.2/B.5 already assert their own outputs; A.2's messaging didn't).
  - Test totals: 27 + 6 + 18 + 3 new integration = **54 new tests for Phase C work** (all green). API suite at 221 passed / 197 skipped (integration). Infra suite still 84 passed.
- **2026-05-13** **Phase D closed.** Two sub-agents fired in parallel: D.1+D.3 combined (worker GitHub auth, tightly coupled) and D.2 (local-adapter dev-local mode).
  - **D.1+D.3 combined** (worker auth): `repo_mode == "github"` re-introduced in `git.py` — `clone` shells plain `git clone https://github.com/<owner>/<repo>.git` (no token in URL) and `open_pr` shells `gh pr create` (no `GH_TOKEN` env). New module `treadmill_agent/startup_auth.py` owns the secrets-fetch + `gh auth login --with-token` (via stdin pipe — PAT lives in the kernel pipe buffer for the duration of one syscall, then is gone) + `gh auth setup-git` sequence. Bootstrap-vs-worker credentials resolved cleanly: `bootstrap_session` uses host credentials to fetch the `worker_aws_credentials_secret_name`; `worker_session` is rebuilt with the fetched keys; documented in a comment. When `worker_aws_credentials_secret_name` is unset, the default credential chain handles AWS calls directly. Fail-fast on every error path. New settings: `github_pat_secret_name`, `worker_aws_credentials_secret_name`, both `str | None = None`. 120 worker tests pass (some via stub `gh` + `git` binaries on PATH; PAT-leak-sentinel regression test asserts the PAT value never appears in any recorded argv, env, or URL). **Banked for D.2 / Phase E**: `gh auth login --with-token` writes to `~/.config/gh/hosts.yml`; in a one-shot worker container this is fine (re-bootstrap per task), no persistent volume needed. **Note**: pre-B.7-closure github-mode code didn't exist — the prior path was a clean `raise`, so the re-implementation is greenfield against the spec.
  - **D.2** (local-adapter dev-local): `--deployment / -d <id>` flag added to `up`, `down`, `status`, `run-worker`. `load_deployment_yaml(deployment_id)` validates the top-level + sub-block schema. `_up_dev_local()` skips moto + skips `cdk synth`; reads container env from YAML. `_volumes_for()` mounts `~/.aws:/root/.aws:ro` (both API and worker Dockerfiles run as root — verified). Env-var names match `config.py` exactly: API uses some `TREADMILL_*`-prefixed + some unprefixed-via-alias (per pydantic-settings); worker is all unprefixed-via-raw-os.environ. **D.2 made working-name guesses for D.1+D.3's settings** (`GITHUB_PAT_SECRET_NAME`, `WORKER_AWS_CREDENTIALS_SECRET_NAME`, `REPO_MODE`); guesses turned out to match D.1+D.3's actual implementation. 97 adapter tests pass (24 new in `test_runtime_dev_local.py`); the no-`--deployment` fully-local path is verified unchanged by `test_up_fully_local_path_unchanged`. **Banked for Phase E**: autoscaler doesn't run in dev-local mode (worker is launched via `run-worker --deployment <id>` for now). When dev-local moves to a full task-driven autoscaling shape, revisit.
  - Cross-suite verification: worker 120 / adapter 97 / API 221 / infra 84 = **522 tests green** across the codebase post-D.
- **2026-05-13** **Cold-start gotcha closed.** Phase A.3 banked a finding that `cdk.json` invoked `python -m treadmill_infra.app`, which fails on a fresh shell where `python` (not `python3`) isn't on PATH. Fixed in `infra/cdk.json` by changing the app invocation to `uv run --package treadmill-infra python -m treadmill_infra.app`. Verified: bare `cdk ls --context mode=dev_local --context deployment_id=personal` and `cdk synth` now work without `uv run cdk ...` wrapping. Infra tests still 84/84. Phase E.1 walkthrough simplifies: operators run `cdk deploy ...` directly.
- **2026-05-13** **Phase E.1 first-cut: bootstrap + deploy + secret + init landed.** Personal AWS member account `000000000000` set up via IAM Identity Center; operator SSO profile `treadmill-personal` configured. Region: **us-west-2** (operator preference; matches existing AWS habit). CDK bootstrap clean (12/12 resources). `TreadmillPersonalCloudLite` deployed (24/24 resources, 88s). Webhook HMAC secret generated locally + put via `aws secretsmanager put-secret-value`. `treadmill-local init personal` produced `~/.treadmill/personal.yaml` matching ADR-0016's schema exactly.
- **2026-05-13** **Real bug fixed mid-deploy: B.2's `ForceDeleteWithoutRecovery` was invalid CFN.** First deploy attempt failed with "Unsupported property [ForceDeleteWithoutRecovery]" on all three secrets. Root cause: B.2's agent attempted to express force-delete-without-recovery via a CFN property override on `AWS::SecretsManager::Secret`, but that flag is a parameter on the `DeleteSecret` API call only — there is no equivalent CloudFormation property. The runbook in this plan already handles it correctly via `aws secretsmanager delete-secret --force-delete-without-recovery` run *before* `cdk destroy`. Fixed `infra/treadmill_infra/constructs/secrets.py` to drop the property override (kept `removal_policy=DESTROY`); replaced the obsolete `test_every_secret_has_force_delete_without_recovery` assertion with a regression-net `test_no_secret_sets_force_delete_without_recovery` so the wrong shape can't re-land. 84/84 infra tests green after fix; second deploy attempt succeeded. **Lesson**: CDK assertion-only tests don't catch "this CFN property doesn't exist" — you only learn that at deploy time. A future cleanup phase could run `cdk synth` followed by `aws cloudformation validate-template` in CI to catch invalid CFN before deploy.
- **2026-05-13** **Region drift surfaced.** ADR-0016's prose canonicalized `us-east-1` (so the `AWS/Billing` CloudWatch namespace would deliver data to the B.5 billing alarm). Operator chose us-west-2 (existing AWS habit). The billing alarm provisioned cleanly in us-west-2 but is silent — `AWS/Billing` is only published in us-east-1. Acceptable trade-off at v0; alternatives are a cross-region stack-set (over-engineering) or amending ADR-0016 to drop the us-east-1 prescription (cleaner). Banked for an ADR-0016 amendment in the next housekeeping pass.
- **2026-05-13** **Phase E.1 part-2 closed: secrets populated + stack up + worker bootstrap verified.** Worker IAM user `treadmill-personal-worker` created with least-privilege policy (consume work queue + publish events + read github-pat — three statements, three ARNs, nothing else); access key generated and stored in `treadmill-personal/worker-aws-credentials` without ever touching disk outside the AWS API call. GitHub fine-grained PAT stored in `treadmill-personal/github-pat`. SNS email subscription for `josephlepper@gmail.com` pending operator confirmation in inbox. GitHub repo `joeLepper/treadmill` created (private); webhook installed (ID 622339829) pointing at the API Gateway URL with the HMAC secret pre-loaded. GitHub's first `ping` traveled the entire AWS path (GitHub → APIGW → Lambda → SQS), returning 202.
  - **`treadmill-local up --deployment personal`** stood up Postgres + Redis + API containers cleanly. `/health/ready` shows `postgres + redis + coordination_consumer + webhook_inbox_poller` all `ok`. Worker boots through the full Phase-D auth path: SSO bootstrap session → fetch worker AWS creds → rebuild worker session → fetch GitHub PAT → `gh auth login --with-token` (stdin pipe; PAT never on disk) → `gh auth setup-git`. End-to-end queue + DB plumbing verified live in AWS.
  - **Three friction points discovered during first deploy — banked for follow-up:**
    1. **Local-adapter doesn't auto-rebuild Docker images**: `treadmill-api:dev` and `treadmill-agent:dev` were stale (built before A.1 / D.1+D.3) and `up` happily ran them, hiding the new code. Fix: stamp image with git rev OR rebuild on `up` if Dockerfile or source mtime is newer than image. Until then, the runbook needs an explicit "rebuild before first deploy after pulling new code" step.
    2. **Local-adapter doesn't auto-run alembic migrations on dev-local up**: fresh Postgres is schema-less; the API errors with "relation events does not exist" until `docker exec treadmill-api alembic upgrade head` is run manually. Fix: invoke alembic in the up flow after Postgres is healthy.
    3. **API has no `logging.basicConfig`**: app loggers default to WARNING, so INFO from the webhook poller + coordination consumer + replay loop is invisible. Diagnostics-blind in dev. Fix: configure root logger in `app.py` or the CLI entrypoint to emit INFO+ to stdout.
  - **One real bug fixed mid-deploy** (already captured above): B.2's `ForceDeleteWithoutRecovery` property override was invalid CFN — that's a property on the API call, not the resource. Removed; regression-net test added.
  - Phase 2 success criteria 2 + 4 + 5 + 8 are blocked behind one remaining task: the end-to-end smoke (submit → real PR → webhook → trigger). All preconditions are met.
- **2026-05-13 (18:16 UTC)** **🎉 End-to-end smoke landed.** The full chain works against real AWS + real GitHub.
  - Scenario-1 (doc-driven) plan submitted via `treadmill plan submit --doc /tmp/smoke-plan.md --repo joeLepper/treadmill` → wf-author run dispatched to SQS work queue.
  - Worker (`treadmill-local run-worker treadmill-agent --deployment personal`) booted, fetched IAM creds + PAT, `gh auth login`, cloned the repo, ran Claude Code (claude-haiku-4-5, ~14s), wrote SMOKE.md, committed, pushed branch, `gh pr create` opened PR #1.
  - GitHub fired `pull_request:opened` webhook → API Gateway → Lambda → SQS inbox → poller dequeued → persisted `events(entity_type=github, action=pr_opened)` row → published to SNS events topic → coordination consumer projected → trigger evaluator fired → wf-review run created with `trigger=webhook:pr_opened` → wf-review's first `step.ready` event landed in events table → wf-review work-queue message dispatched.
  - Phase 2 success criteria 2 + 4 + 5 + 8 satisfied.
  - **Two findings on the way:**
    - `gh repo create` produces an empty repo (no main branch). First worker run failed on `origin/main not a commit`. Fixed manually by pushing an initial commit; should be part of the dev-local first-deploy runbook.
    - Initial PAT scopes were insufficient (clone OK, push 403). Operator widened to Contents:rw + Pull requests:rw; second submit succeeded.
  - **Intent-only-submit gap discovered**: `treadmill submit "..." --repo ...` (no `--doc`) creates a plan in `registered` state. The dispatch path checks the plan-active gate; only Scenario 1 (`--doc`) or `--dev` (fully_local only) emits `PlanActivated`. In dev_local, an intent-only submit therefore creates a task whose run is permanently deferred — no worker can pick it up. Two paths forward (future work):
    1. Have intent-only-without-`--dev` spawn a wf-plan task whose product is a plan-doc PR; merging that PR emits PlanActivated, unblocking the implicit wf-author. This is the "real" production flow.
    2. Loosen the `--dev` gate so it works in dev_local too. Friction-only fast-path; not gated to fully_local.
  - **The PAT pasted into this session's transcript should be rotated** at operator's convenience. Conversation history is within the operator's trust boundary, but fine-grained PATs are revocable + scoped so the right hygiene is to rotate after the smoke.
- **2026-05-13** **Friction-point cleanup closed.** Two parallel agents landed:
  - **API entrypoint** (`services/api/treadmill_api/cli.py`): now calls `logging.basicConfig(level=settings.log_level)` + `_run_migrations(settings)` before `uvicorn.run`. Settings gain `skip_migrations: bool = False` (env `TREADMILL_SKIP_MIGRATIONS`) and `log_level: str = "INFO"` (env `TREADMILL_LOG_LEVEL`). Alembic runs in-process (not shelled out); fails fast on migration error. 7 new tests; 255 passed total in the API suite.
  - **Local-adapter auto-rebuild** (`tools/local-adapter/treadmill_local/runtime.py` + `cli.py`): `up` and `run-worker` now invoke `docker build` for `treadmill-api:dev` and `treadmill-agent:dev` before starting containers; cached builds are sub-second no-ops. Repo-root discovery via `Path(__file__).resolve().parents[3]` with a marker-validation fallback (looks for `[tool.uv.workspace]` in the discovered pyproject.toml or sibling `infra/cdk.json`). Output captured to keep up's progress block clean; on failure both streams dumped. `--no-build` opt-out flag for cases where the operator wants to use a known-good cached image. 13 new tests; 110 passed total in the adapter suite.
- **2026-05-13** **ADR-0016 amended** to capture (a) regional flexibility (no us-east-1 prescription; billing alarm degrades to no-op in non-us-east-1 regions), (b) the empty-repo `gh repo create` gotcha, and (c) the PAT-write-scopes requirement. These were operator-runbook items that the ADR's prose didn't address; surfaced on first real deploy.
- **2026-05-13** **Treadmill in git.** `/home/joe/treadmill` initialized as a git repo (was a non-tracked working dir until this point). Initial commit `f0ece44` covers 244 files / 51k lines through Phase 2 closure. Pushed to `joeLepper/treadmill`; smoke PR #1 closed (artifact). Identity scoped to repo: Joe Lepper <josephlepper@gmail.com>. From here forward, every change has a commit; "Treadmill builds Treadmill" has a substrate to work on.
- **2026-05-13** **ADR-0018 drafted (proposed): Autoscaler in dev-local mode.** Reuses the existing `Autoscaler` class; only wiring changes. New `autoscaler:` block in deployment YAML. Implementation is gated on ADR-0019 (the SSO-mount failure would silently break auto-spawned workers).
- **2026-05-13** **ADR-0019 drafted (proposed) + implemented: Host-side credential injection.** Supersedes ADR-0016 Q16.c's `~/.aws`-mount + bootstrap-session path with env-var injection from the host. Worker `startup_auth.resolve_worker_aws_session` collapses to `boto3.Session(region_name=...)`. The `~/.aws` mount is gone in dev-local. SSO-expired errors surface clean (with `aws sso login` remediation hint). Local-adapter 113 passed; worker 117 passed.
- **2026-05-13** **ADR-0020 drafted (proposed): Observability via OpenTelemetry + Grafana.** Resolves the bunkhouse-precedent pain point that Joe named (Claude Code worker output was never observable): subprocess.Popen + line-streaming + per-line OTel log emission tagged with task/step/role context, visible live in Grafana during a 30-60s Claude Code run. Local stack: 5 new containers (grafana/loki/prometheus/tempo/otel-collector). Production stack: same emitter, different collector destination (Grafana Cloud or AWS-managed; decision deferred to TreadmillCloudFull ADR). Token tracking via `claude --output-format json` parse; bonus if available. ADR-0020 is not blocked by 0018/0019; the Claude Code streaming fix could ship first as a quick win.
- **2026-05-13** **ADR-0018 implemented: autoscaler in dev-local mode.** Wiring only — the existing `Autoscaler` class is unchanged. `deployment_config.py` accepts an optional `autoscaler:` block with defaults (min=0, max=1, tick_seconds=5). `_start_autoscaler_dev_local` spawns the subprocess with YAML-derived env + a new `TREADMILL_AUTOSCALER_DEPLOYMENT_ID` env var. The subprocess's `main()` branches on that var: when set, constructs `LocalRuntime(deployment_config=cfg)` so spawned workers get ADR-0019's credential injection automatically. `_stop_autoscaler` shared between fully-local and dev-local. `--no-autoscaler` flag on `up` for debug/manual modes. `AWS_ENDPOINT_URL` defensively popped to prevent moto leak from a previous fully-local session. Local-adapter tests: 129 passed (+18 new). With this landed, the next end-to-end smoke is "submit + walk away" — Treadmill processes the chain hands-off.
- **2026-05-13** **ADR-0021 drafted (proposed): plan-merge-to-main as submission trigger.** Closes the loop on "Treadmill builds Treadmill" — operator/agent writes a plan doc as a PR, review/approval gates it, merge to main becomes the submission signal. New normalizer rule on `pull_request:closed:merged` filtered by `docs/plans/*.md` emits a `plan_doc_merged` verb. Trigger handler fetches the doc via gh API, parses frontmatter, and (when `status: active`) reuses the existing Scenario-1 plan-creation machinery. Plan ID is deterministic from `repo + path + merge_commit_sha`. CLI submission survives as a backstop. Implementation depends on the existing webhook ingestion path (ADR-0017) + the normalizer + a new trigger handler in the consumer. Not blocked by anything; can implement in parallel with ADR-0020 observability work.
- **2026-05-13** **ADR-0022 + ADR-0021 implemented + smoked end-to-end.** Two parallel implementation agents landed both ADRs in one round; the live smokes proved the system operational:
  - **Smoke A (validates ADR-0022)**: `treadmill plan submit --doc /tmp/smoke-plan.md` → wf-author worker auto-spawned → opened PR #3 with SMOKE.md → webhook fired → wf-review worker auto-spawned (via ADR-0018 autoscaler) → posted a `COMMENTED` review on PR #3. Total: 58 seconds. The fix: ADR-0022's per-kind dispatch routed role-reviewer through the new `review` disposition handler (`gh pr review` instead of expecting a diff). The earlier "Claude Code produced no changes to commit" failure mode is gone.
  - **Smoke B (validates ADR-0021)**: cut `docs/plans/2026-05-12-smoke-b-take-2.md` with `status: active` frontmatter, opened PR #5, merged via `gh pr merge --squash`. **No further operator action.** 8 seconds later, autoscaler spawned wf-author worker; 28 seconds later it had opened PR #6 with `docs/SMOKE_B.md`; 21 seconds after that wf-review posted a review on PR #6. **Total elapsed from merge-of-plan-doc to review-on-implementation-PR: 56 seconds, zero CLI invocations.**
  - **Friction surfaced + fixed mid-smoke (commit 8b0f63c)**: the first Smoke B attempt silently no-op'd because the local-adapter wasn't injecting `GITHUB_TOKEN` into the API container's env. Without it, the API's `github_client` is None, the plan-doc handler skips its doc-fetch, and the chain halts at "pr_merged event observed, no follow-up." Fix: `_fetch_github_pat` mirrors ADR-0019's host-side fetch pattern; PAT comes from the same Secrets Manager entry the worker uses.
  - **Banked**: task #102 — `role-reviewer` prompt writes from author POV ("created SMOKE.md...") instead of reviewer POV. The runner machinery is right; the prompt needs a sentence about "evaluate" vs "summarize." Small follow-up.
  - **Phase 2 success criterion 4 (end-to-end PRs) is now demonstrably-automated *with plan-doc-merge as the trigger*** — the closed loop Joe described as "the dream end-state."

## Open questions

These came out of the adversarial review (2026-05-12). Resolve as Phase A/B/C work surfaces them; do not block on them at plan-acceptance time.

- **Q4.a — Does Postgres in `dev_local` mode run in a Docker container alongside the API, or natively on the laptop?** Currently the plan reads "Postgres + Redis + API as Docker containers." That works on macOS + Linux. The trade-off is: container-Postgres survives laptop reboots only via a named volume (which the plan must specify in C.2's container config); native Postgres survives reboots automatically but adds an install dependency. Resolve in C.2 by either documenting the named volume or switching to native.
- **Q4.b — Does the worker run as a Docker container or a long-lived process on the laptop?** Today's local-adapter runs the worker as a container; that should continue, but in `dev_local` mode the container must mount the operator's `~/.aws/` so boto3 picks up the SSO profile (or the IAM-User keys from `WORKER_AWS_CREDENTIALS_SECRET_NAME`). Spelled out in D.2; verify during code review that the AWS profile resolution path actually works inside the container.
- **Q4.c — Should `treadmill-local init` auto-install the GitHub webhook via `gh api`, or is that operator-manual?** Currently E.2 is manual. Auto-install would close one rough edge but requires the init command to know the GitHub repo identity (operator passes `--repo <owner>/<name>`). Tractable; defer until the manual path proves friction.
- **Q4.d — How does the operator change the billing-alarm threshold post-deploy?** Currently the threshold is a CDK context value; changing it requires a `cdk deploy`. Acceptable for v0; if the alarm fires too often, the operator can either raise the threshold (`cdk deploy --context billing_alarm_threshold=20`) or unsubscribe from the SNS topic.
- **Q4.e — Does the `WebhookInboxPoller` health probe block `/healthz` readiness?** Per Phase 3's coordination-consumer pattern, `/healthz` reports `503` when any registered probe is unhealthy. Should the webhook poller failing (e.g., AWS credentials expired) take the entire API offline? Tentative answer: yes — if the webhook ingress is broken the API has lost a load-bearing capability and downstream readiness probes should know. Validate in C.2 by writing the test that asserts "kill the SQS connection → `/healthz` flips to 503 within N seconds."
- **Q4.f — What's the recovery path when the laptop is offline for >14 days (the queue retention window)?** The SQS retention is 14 days; webhooks beyond that drop. GitHub's webhook redelivery only goes back ~7-30 days depending on event type. The honest answer for v0 is "Joe doesn't go offline that long; if he does, missed events are recovered via `gh pr list` + manual replay." Document this constraint in the operator guide rather than engineering around it.

## Post-mortem

(To be filled in when this plan transitions to `completed` or `abandoned`.)
