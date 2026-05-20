# ADR-0016: Dev-local deployment topology

- **Status:** accepted — amended by ADR-0049 (GitHub-App vs PAT clause, Q16.d)
- **Date:** 2026-05-12
- **Related:** ADR-0002, ADR-0007, ADR-0011, ADR-0017

## Context

Phase 2 closed with a fully-local substrate — `treadmill-local up` runs moto + Docker containers, no real AWS. Half of Phase 2's success criteria (Scenario 2 plan-doc PR-merge gate, end-to-end task execution producing real PRs, every starter workflow firing at least once, Treadmill submits its own first PR) require real GitHub webhook delivery. GitHub fires webhooks at a stable public URL; Joe's laptop doesn't have one.

This ADR commits to a third deployment mode — **"dev-local"** — alongside the existing fully-local mode. The shape:

- **AWS holds only what needs a stable URL or a multi-consumer pub-sub substrate.** Queues, topics, secrets, the API Gateway that receives webhooks, an IAM role or two. ~$2/month per deployment.
- **Compute runs locally.** API service, Postgres, Redis, and workers stay on the laptop or Joe's home machine. They talk to AWS via boto3 over the public internet using the AWS SDK's default credential chain.
- **Multi-tenant by separate AWS accounts.** Joe needs to be able to truthfully claim no employer money paid for personal work; account-level isolation is the load-bearing claim that makes that defensible.

The user-blessed motivation (2026-05-12): *"I need to be able to deploy instances of Treadmill that silo the work that flows through them from different employers. One of those silos is for my personal work and should be deployed on as little aws as possible."*

A fully-cloud deployment (RDS, ElastiCache, ECS Fargate, ALB) is rejected for v0 — it's ~$150–250/month per deployment per bunkhouse's cloud-native footprint, and the personal-Treadmill-on-Joe's-laptop case doesn't justify that cost.

A pure-tunnel deployment (Cloudflare Tunnel / ngrok bridging the laptop API to a public URL) is rejected because laptop downtime drops webhooks; AWS-side queue buffering survives 14 days of laptop-closed time without GitHub retries timing out.

## Decision

### Three deployment modes; one CLI; one CDK app

Treadmill ships three deployment modes, each with its own CDK stack class and its own `treadmill-local` invocation:

| Mode | CDK stack | What's in AWS | What's local |
|---|---|---|---|
| `fully_local` | none (moto only) | nothing | moto + Postgres + Redis + API + workers |
| `dev_local` | `TreadmillCloudLite` | queues + topics + secrets + API Gateway + webhook Lambda | Postgres + Redis + API + workers |
| `fully_remote` | `TreadmillCloudFull` (future) | everything: RDS, ElastiCache, ECS Fargate, ALB | nothing |

This ADR commits Treadmill to landing `fully_local` (already shipped) and `dev_local` (new). `fully_remote` is named here for forward-compatibility but is explicitly out of scope until evidence demands it.

### CDK stack split

`infra/treadmill_infra/stacks/spike.py` is renamed and split. The constructs that exist in both `dev_local` and `fully_remote` modes — Messaging, Secrets, the Webhook Receiver — move into shared constructs under `infra/treadmill_infra/constructs/`. The stacks become:

- **`TreadmillCloudLite`** (new — Python class name) — composed of `MessagingConstruct` + `SecretsConstruct` + `WebhookReceiverConstruct` (per ADR-0017). Deploys to `dev_local` deployments. ~10 AWS resources.
- **`TreadmillCloudFull`** (future) — composed of the same plus `DatabaseConstruct` + `CacheConstruct` + `ComputeConstruct` + `LoadBalancerConstruct`. Deploys to `fully_remote` deployments. Not built in this ADR.

**The CloudFormation stack name** is `Treadmill<PascalCaseDeploymentId>CloudLite` — e.g. `TreadmillPersonalCloudLite`, `TreadmillStrongdmCloudLite`. The Python class is always `TreadmillCloudLite`; the *stack name* (the CFN identifier passed to `cdk deploy`) is derived from `deployment_id`. This means two deployments can land in the same AWS account without stack-name collision (e.g. if Joe spikes a third deployment for testing in his personal account). Resource names inside the stack also carry the deployment suffix: `treadmill-personal-events`, `treadmill-personal-webhook-inbox`, etc.

The `infra/app.py` entry dispatches on a CDK context flag: `cdk deploy Treadmill<DeploymentId>CloudLite --context deployment_id=personal --context mode=dev_local` synths and deploys. The canonical-spelling rule (below) commits to `dev_local` everywhere — snake_case literal, not the kebab-case noun-phrase `dev-local` we use in prose.

This deviates from bunkhouse's monolithic single-stack pattern. The reason: bunkhouse runs one deployment per AWS account; Treadmill explicitly allows the same AWS account to host multiple deployments if Joe wants, AND must support different accounts per deployment. Separate stack classes are clearer than conditional resource creation, and they keep `cdk synth` diffs readable.

### Canonical spellings (one literal per surface)

The deployment mode flows through CLI, CDK context, env var, YAML, and a Python enum. Reviewers were finding two and three spellings within a single decision; this ADR pins the canonical form so the next reviewer doesn't have to.

| Surface | Literal | Example |
|---|---|---|
| Python enum member | `DEV_LOCAL` (UPPER_SNAKE) | `DeploymentMode.DEV_LOCAL` |
| Python enum value (str-typed) | `dev_local` (lower_snake) | `DeploymentMode.DEV_LOCAL.value == "dev_local"` |
| Env var name | `TREADMILL_DEPLOYMENT_MODE` (UPPER_SNAKE) | — |
| Env var value | `dev_local` (lower_snake) | `TREADMILL_DEPLOYMENT_MODE=dev_local` |
| CDK context flag | `dev_local` (lower_snake) | `--context mode=dev_local` |
| YAML field key | `deployment_mode` (lower_snake) | `deployment_mode: dev_local` |
| Prose / docs noun | `dev-local` (kebab) | "the dev-local deployment mode" |

Same pattern for `fully_local` and `fully_remote`. Reviewers reject mixing.

The `deployment_id` value (`personal`, `strongdm`, etc.) is constrained to `^[a-z][a-z0-9]{0,29}$` — lowercase alphanumeric, 1-30 chars, must start with a letter. The PascalCase form for stack names (`TreadmillPersonalCloudLite`) is derived by capitalizing the first letter. The 30-char limit leaves headroom for the longest resource-name suffix (`treadmill-<deployment_id>-webhook-inbox-dlq` = 45 chars at the max; well under SQS's 80-char limit + FIFO `.fifo` suffix where applicable).

### Multi-tenant via separate AWS accounts

Each deployment lives in its own AWS account. The reason: IAM roles within a single account don't satisfy the cost-attribution claim (Cost Explorer aggregates per-account, not per-IAM-role), don't provide blast-radius isolation against credential compromise, and don't survive the "I'm being audited for whether I used employer resources for personal work" question. **Account-level isolation is the load-bearing claim that makes the multi-tenant story defensible.**

Implications:

- **AWS profile per deployment (operator-side).** Joe configures `~/.aws/config` with one profile per deployment (`treadmill-personal`, `treadmill-strongdm`, etc.). The CLI and CDK reads `~/.treadmill/<deployment_id>.yaml` to discover the right profile and sets `AWS_PROFILE` before constructing boto3 clients. boto3's default credential resolution chain honors this with no custom plumbing. **SSO is fine for operator-side** because operator commands are short (seconds to minutes); SSO session expiry mid-command is rare and surfaces as a clear `aws sso login` prompt.
- **Long-lived IAM-User keys per deployment (worker-side).** The worker is a long-running boto3 client; SSO session expiry mid-step would silently break SNS publishes mid-workflow. The CDK provisions a per-deployment IAM User (`treadmill-<deployment_id>-worker`) with scoped policy (SNS publish on events topic; SQS consume on work queue; SQS send on dispatcher queue; Secrets Manager read on `treadmill-<deployment_id>/*`). The user's access key + secret get written to Secrets Manager at `treadmill-<deployment_id>/worker-aws-credentials`; the worker reads them at startup. **This cribs bunkhouse's Jan 29 IAM-User-with-access-key pattern.** Operator credentials stay SSO; worker credentials are long-lived. Documented trade-off: long-lived keys mean rotation is operator-driven (`cdk deploy` rolls keys, worker restart picks up the new ones).
- **CDK deploy is per-account.** `cdk deploy Treadmill<DeploymentId>CloudLite --context deployment_id=personal --context mode=dev_local --profile treadmill-personal` against Joe's personal account. The same CDK code runs against the employer account with `--profile treadmill-strongdm`. Stack names + resource names both carry the `deployment_id` suffix so an operator running both deploys from the same shell doesn't get confused — and even if the operator typos the profile flag, the resource-name-collision guard fires (next bullet).
- **Account-ID assertion preflight (the cause-mitigation).** A typo in `--context deployment_id` would otherwise deploy a personal stack into the wrong account, blending deployments. To prevent this: `treadmill-local init` and `treadmill-local up --deployment <id>` both run `aws sts get-caller-identity --profile <yaml.aws_profile>` and assert the returned `Account` matches the YAML's `aws_account_id` field (added to the schema below). Mismatch is a hard error. `cdk deploy` itself doesn't enforce this directly, so the discipline is: the operator runs `treadmill-local init` first against a fresh deployment to populate the YAML; subsequent operations are guarded.
- **Cost attribution backstop.** Every CDK construct adds `Tag.add("Project", "treadmill")` + `Tag.add("Deployment", deployment_id)`. Account isolation is the primary claim; tags are the in-account fallback if a future ADR collapses multiple deployments into one account. CDK assertion tests in `infra/tests/` enforce both tags exist on every taggable resource.
- **CloudWatch billing alarms** at $5/month per deployment, provisioned by a `BillingAlarmConstruct` composed into `TreadmillCloudLite`. Cheap insurance against runaway costs.

### Per-deployment config at `~/.treadmill/<deployment_id>.yaml`

CDK outputs (the resolved ARNs, queue URLs, secret names) get written to a per-deployment config file outside the repo. The file is operator-managed; the repo carries only the CDK code (which is reproducible by definition) and the `Settings` schema. Schema:

```yaml
deployment_id: personal
deployment_mode: dev_local
aws_profile: treadmill-personal
aws_region: us-east-1
aws_account_id: "111111111111"   # asserted at every operation that touches AWS

aws:
  events_topic_arn: arn:aws:sns:us-east-1:111111111111:treadmill-personal-events
  events_queue_url: https://sqs.us-east-1.amazonaws.com/111111111111/treadmill-personal-coordination
  work_queue_url: https://sqs.us-east-1.amazonaws.com/111111111111/treadmill-personal-work.fifo
  webhook_inbox_queue_url: https://sqs.us-east-1.amazonaws.com/111111111111/treadmill-personal-webhook-inbox
  webhook_inbox_dlq_url: https://sqs.us-east-1.amazonaws.com/111111111111/treadmill-personal-webhook-inbox-dlq
  webhook_api_url: https://abc123.execute-api.us-east-1.amazonaws.com

secrets:
  github_webhook_secret_name: treadmill-personal/github-webhook-secret
  github_pat_secret_name: treadmill-personal/github-pat
  worker_aws_credentials_secret_name: treadmill-personal/worker-aws-credentials

local:
  database_url: postgresql+asyncpg://postgres:postgres@localhost:15432/treadmill
  redis_url: redis://localhost:16379/0
  api_url: http://localhost:8088
```

YAML rather than TOML — operator preference. The file lives outside the repo (`~/.treadmill/`) rather than at `config/personal/` to avoid leaking deployment-specific names + ARNs into git history. Each operator who deploys their own instance has their own per-deployment files.

### `treadmill-local init <deployment_id>` populates the config from CDK outputs

Bunkhouse omits this step — operators edit `~/.bunk/config.yaml` by hand. **Treadmill improves on this.** A new CLI subcommand `treadmill-local init <deployment_id> --stack-name <stack-name> --profile <aws-profile>` runs `aws cloudformation describe-stacks --stack-name <stack-name>` to read the deployment's CloudFormation outputs, then writes the YAML. Idempotent and re-runnable; CDK redeploys that change ARNs update the file in place.

### `Settings.local: bool` generalizes to `DeploymentMode`

The current binary `Settings.local: bool` doesn't capture three modes. Replace with:

```python
class DeploymentMode(str, Enum):
    FULLY_LOCAL = "fully_local"      # moto + local docker; AWS_ENDPOINT_URL set
    DEV_LOCAL = "dev_local"          # real AWS queues + local docker; AWS_ENDPOINT_URL unset
    FULLY_REMOTE = "fully_remote"    # real AWS everything; future
```

`Settings.deployment_mode` replaces `Settings.local`. The existing `--dev` flag's gating logic ("honor `--dev` when running locally") generalizes to "honor `--dev` when `deployment_mode in {FULLY_LOCAL, DEV_LOCAL}`" — both local modes are operator-driven; only `FULLY_REMOTE` is production.

`Settings.aws_endpoint_url` keeps its current meaning: when set (fully_local with moto), boto3 routes there; when unset (dev_local or fully_remote), boto3 talks to real AWS.

**New `Settings` fields for dev_local:**

- `webhook_inbox_queue_url: str | None` — the SQS queue the API's webhook-inbox poller (per ADR-0017) drains. Aliased to env `WEBHOOK_INBOX_QUEUE_URL`. Required in `dev_local` and `fully_remote` modes; unset in `fully_local`.
- `aws_account_id: str | None` — the AWS account the deployment lives in. Aliased to env `AWS_ACCOUNT_ID`. Used for the preflight assertion (`sts get-caller-identity == aws_account_id`); required in `dev_local` and `fully_remote`.
- `deployment_id: str | None` — the deployment slug; aliased to env `TREADMILL_DEPLOYMENT_ID`. Required in `dev_local` and `fully_remote`.

All three fields default to `None` to preserve backward compatibility with `fully_local` mode where they're irrelevant.

### `treadmill-local up --deployment <id>` invocation

The CLI gains a `--deployment` flag:

```
treadmill-local up                          # fully_local default
treadmill-local up --deployment personal    # dev_local against personal-treadmill
treadmill-local up --deployment strongdm    # dev_local against employer-treadmill
treadmill-local down                        # tears down whichever's running
treadmill-local status                      # shows current mode + deployment
```

One adapter, parameterized by mode. The `_ensure_provisioned` path in `tools/local-adapter/treadmill_local/runtime.py` learns two conditional branches:

- **Fully-local**: existing behavior. Start moto, run the provisioner against moto, start local containers with `AWS_ENDPOINT_URL` pointing at moto.
- **Dev-local**: skip moto. Read `~/.treadmill/<deployment_id>.yaml` for the resolved ARNs. Start local containers with env populated from the yaml (real ARNs, `AWS_PROFILE`, `AWS_ENDPOINT_URL` unset).

The worker container's image is the same in both modes. In dev-local mode `AWS_ENDPOINT_URL` is unset and DNS routes boto3 to real AWS.

### Webhook ingestion is a sibling ADR

The webhook ingestion path (API Gateway + Lambda + SQS + local poller) is its own decision with its own alternatives; ADR-0017 covers it. This ADR commits dev-local to *having* a webhook receiver but doesn't decide its shape. The receiver is one of the AWS-side resources `TreadmillCloudLite` provisions.

### What's deferred

- **`TreadmillCloudFull`** — the full-AWS deployment topology for production multi-user. Out of scope until there's evidence Treadmill needs multi-user support. A future ADR commits to the construct set + cost shape.
- **GitHub App vs. PAT.** v0 uses PAT per the 2026-05-12 decision. App migration is a future ADR when multi-user evidence appears.
- **Cross-account CloudWatch aggregation, central observability.** Each deployment owns its own metrics + logs in its own account. A future ADR addresses cross-deployment ops surface.
- **CDK Pipelines** for automated redeploys. Manual `cdk deploy` is fine for v0.

## Bunkhouse precedent

- **Bunkhouse's cloud-native stack runs the API + database + cache in AWS** (ECS Fargate + RDS + ElastiCache, ~$150–250/month). Verified by reading `bunkhouse/infrastructure/lib/bunkhouse-stack.ts` (211 lines) and the 12 construct files under `infrastructure/lib/constructs/`. **Treadmill explicitly does not crib this** — the personal-Treadmill cost target is incompatible with bunkhouse's footprint.
- **Bunkhouse uses one monolithic stack per AWS account.** No per-deployment parameterization, no stack split. **Treadmill diverges** — Treadmill expects multiple deployments per developer, each potentially in different accounts.
- **Bunkhouse uses a single config file** (`~/.bunk/config.yaml`) edited by hand. **Treadmill improves on this** with auto-population via `treadmill-local init`.
- **Bunkhouse's per-entity-type SNS FIFO topics + filtered SQS subscriptions** (8 topics, 6 queues at the time of writing) are a Phase-3+ shape. Treadmill stays with the simpler "one events topic + one work queue + one coordination queue" for now per ADR-0011's "simplest viable shape" posture.
- **Bunkhouse's webhook secret is in env, not Secrets Manager** (per `compute.ts:494-498` — the `apiContainerSecrets` map omits the webhook secret). **Treadmill improves on this** — webhook secret goes in Secrets Manager alongside the GitHub PAT.

## Trade-offs

- **Three modes is more complexity than two.** Mitigation: each mode has a single-line CDK invocation, a single-line CLI invocation, and a single config file. The complexity is at the CDK stack-class level, not in operator commands.
- **Multiple AWS accounts means multiple billing relationships.** Mitigation: Joe owns the personal account; employer accounts are existing. The setup cost is per-account-creation, paid once.
- **Per-deployment config files outside the repo means setup is operator-managed.** Mitigation: `treadmill-local init` automates the population; the operator's only manual step is `aws configure sso --profile <name>` per deployment.
- **The local API still has to be running to drain the webhook inbox queue.** SQS retains for 14 days so laptop-closed-overnight is fine; multi-week downtime would lose events. Acceptable for v0.
- **DNS-based AWS routing from the laptop.** No NAT, no VPC peering, just public-internet calls. Latency is ~50–200ms per call; the substrate isn't latency-sensitive (workflows take seconds-to-minutes).
- **Long-lived worker IAM keys are a security trade-off.** A stolen worker credentials secret grants SNS publish + SQS consume + Secrets Manager read scoped to one deployment. Mitigated by: account-level isolation (a compromised personal-Treadmill key can't touch employer resources); per-deployment scope (the IAM policy names specific ARNs, not wildcards); rotation via `cdk deploy` (the construct provisions a fresh access key; old keys are deleted). Trade-off accepted because the alternative (SSO with auto-refresh on the worker) is meaningfully more operational surface for a single-operator dev tool. A future ADR may move to IRSA + IAM Roles Anywhere when the cost is justified.
- **Single-operator constraint at v0.** Don't `treadmill-local down` one deployment and `up` another while work is in flight. The SQS work-queue claim's visibility timeout extends past the swap; the message redelivers onto a queue no worker is currently polling, and the step's `step.ready` event sits with no consumer until the original deployment comes back up. Acceptable for a single-developer dev tool; documented in `treadmill-local status` output ("active deployment: N work-queue claims in flight; downing now will defer those steps to the next `up`").
- **Stack deletion has a Secrets Manager recovery-window gotcha.** `cdk destroy` schedules secret deletion with a 7-30 day recovery window by default; the second `cdk deploy` then fails because the secret name is still reserved. Mitigated by setting `removalPolicy=DESTROY` + `forceDeletion=True` on every `Secret` in `TreadmillCloudLite` (so destroy is immediate). Documented in the operator runbook.

## Alternatives considered

- **Fully-AWS deployment for personal-Treadmill.** Rejected: ~$150–250/month per deployment is prohibitive for a personal tool that idles 90% of the time. Bunkhouse's cost shape is appropriate for multi-user production, not single-user development.
- **Pure-tunnel deployment** (Cloudflare Tunnel / ngrok bridging localhost to a public URL). Rejected: laptop-online dependency for webhook delivery; tunnel-provider outage = lost events; auth-credential management for the tunnel adds operational surface; ngrok's free tier has session limits that would break Joe's actual workflow. SQS buffering is the cleaner mechanism for "laptop sometimes online."
- **GitHub polling instead of webhooks** (bunkhouse-local's pattern; bunkhouse polls every 60s in the VPN-only deployment). Rejected: 60s latency per poll × N repos × M event types scales poorly; webhook-driven event delivery is the bunkhouse-cloud-native pattern Treadmill aligns with on every other axis. The cost saving is illusory since polling burns API quota.
- **Shared AWS account with IAM-role-per-deployment isolation.** Rejected: Cost Explorer aggregates per-account, not per-IAM-role; "no employer dollars paid for personal work" becomes contested if both deployments live in the same account. Account separation is the load-bearing claim.
- **In-repo per-deployment config** (`config/personal/treadmill.yaml`). Rejected: leaks deployment-specific resource names + ARNs into git history; if Joe shares the repo (open-source the substrate, contribute to it from work), personal-deployment names appear in commit logs. Outside-repo is private by construction.
- **Single stack class with conditional resource creation** controlled by CDK context flags. Rejected: `if (mode === 'dev-local')` branches multiply across constructs as the stack grows; `cdk synth` diffs become noisy; reviewers can't easily see "what does dev-local actually deploy?" Separate stack classes are cleaner.
- **One adapter with hardcoded mode** (no `--deployment` flag; one operator runs one mode at a time via env vars). Rejected: Joe explicitly wants to run personal-Treadmill and employer-Treadmill in alternating sessions; a CLI flag is the cleanest dispatch.

## Open questions

- **Q16.a — Should `~/.treadmill/<deployment_id>.yaml` be encrypted at rest?** It carries ARNs but no secrets (secrets resolve via Secrets Manager at runtime). v0 leaves it unencrypted; future ADR if evidence of need.
- **Q16.b — Should the CLI command be `treadmill-local up --deployment <id>` or a per-deployment binary symlink (`treadmill-personal up`)?** Symlinks are nicer ergonomics but harder to discover. v0 ships the flag; symlinks are operator-managed if desired.
- **Q16.c — Resolved (but the *how* is superseded by ADR-0019).** Worker identity stays as long-lived IAM-User keys per deployment (`treadmill-<deployment_id>/worker-aws-credentials`); the *delivery mechanism* — host-mount `~/.aws` + bootstrap-session + worker-session — broke under SSO-token refresh in real use. **ADR-0019** replaces it: the local-adapter fetches the secret on the host once per `up` and injects as `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` env vars on every spawned container, with no `~/.aws` mount.

- **Q16.d — Worker `git` authentication for the real Treadmill repo: token-in-URL or `gh auth login`?** Worker has `gh` installed (per `workers/agent/Dockerfile`). Token-in-URL (`https://x-access-token:<PAT>@github.com/<owner>/<repo>.git`) leaks the PAT into the cloned repo's `.git/config` and any subsequent `git remote -v`. `gh auth login --with-token` reads from stdin and stores in `gh`'s config directory (not the repo). **Recommend `gh auth login --with-token <<< $GITHUB_PAT`** at worker startup; `git` operations then route through `gh`'s credential helper. The worker fetches the PAT from Secrets Manager at startup (per the worker-side credential model above) and pipes it into `gh auth login`.

## Region selection

The YAML schema example above uses `us-east-1`. **No region is prescribed by this ADR** — operators choose freely based on existing AWS habits. The one wrinkle is the CloudWatch billing alarm (ADR-0017 / `ObservabilityConstruct`): AWS only publishes the `AWS/Billing` metric namespace in `us-east-1`, so the alarm provisions cleanly anywhere but only delivers data when the deployment lives in `us-east-1`. Operators outside `us-east-1` either accept the alarm as a silent no-op (the construct sets `treat_missing_data=MISSING` so it reports `INSUFFICIENT_DATA` rather than flapping), or set up the billing alarm manually in `us-east-1` independent of the per-deployment stack. Future ADR may carve the billing alarm out into a separate cross-region construct; current call is "single region per deployment, billing alarm degrades gracefully."

In practice the first personal-Treadmill deployment landed in `us-west-2` (operator's existing AWS habit). The deployment is healthy; the silent billing alarm is the only observable cost of the region choice.

## Operator runbook supplements (discovered during first deploy)

The first dev-local deployment surfaced three operator-facing steps that ADR-0016's prose didn't capture:

- **`gh repo create` produces an empty repo** with no default branch. The first `git clone` from a worker fails with `origin/main is not a commit`. Push an initial commit (a README is enough) to seed `main` before the first `treadmill submit`. The `treadmill-local init` flow could plausibly do this; deferred until we've observed the second deploy.
- **Fine-grained PAT scopes must include write access**: at minimum `Contents: Read and write` + `Pull requests: Read and write` on the target repo. Defaulting to read-only causes a 403 at `git push` time, after the worker has already cloned and run Claude Code (so it's a wasted run). Classic PATs with the `repo` scope grant write implicitly.
- **Pushing initial commits and rotating the PAT both happen on the operator's machine, not in CI** — the personal-tier topology has no automation around credential management. Document at the README level whenever there's one.

## Consequences

- **ADR-0017** commits the webhook-receiver shape (API Gateway + Lambda + SQS). This ADR cites the integration but doesn't decide its internals.
- A new transition plan doc — `docs/plans/2026-05-13-week-4-dev-local-deployment.md` — sequences the implementation: CDK split, Settings refactor, init command, webhook poller, GitHub-mode worker, real deployment.
- A future ADR (likely 0018) commits to `TreadmillCloudFull` when multi-user evidence appears.
- The existing `local: bool` setting → `DeploymentMode` enum migration touches every place that reads `settings.local`. Mostly mechanical; tests update in lockstep.
- The `--dev` flag (existing) continues to gate on local-or-dev-local mode; semantics unchanged.
- Phase 2 success criteria 2, 4, 5, 8 become satisfiable once this lands plus a real GitHub repo for Treadmill itself.

## Diagram

```mermaid
flowchart LR
    subgraph Operator["Operator host (laptop)"]
        CLI["treadmill-local CLI"]
        YAML["~/.treadmill/&lt;deployment_id&gt;.yaml"]
        AWSProfile["~/.aws (SSO profile<br/>treadmill-&lt;deployment_id&gt;)"]
        subgraph LocalContainers["Local containers (docker)"]
            API["Treadmill API"]
            Postgres["Postgres"]
            Redis["Redis"]
            Worker["treadmill-agent worker"]
        end
    end

    subgraph AWS["AWS account (per deployment)"]
        subgraph CloudLite["TreadmillCloudLite stack<br/>(Treadmill&lt;DeploymentId&gt;CloudLite)"]
            APIGW["API Gateway HTTP API<br/>POST /webhook/github"]
            WebhookLambda["webhook-receiver Lambda"]
            WebhookInbox["SQS: webhook-inbox"]
            EventsTopic["SNS: events topic"]
            WorkQueue["SQS: work (FIFO)"]
            CoordQueue["SQS: coordination"]
            WorkerCreds["Secrets Manager:<br/>worker-aws-credentials"]
            WebhookSecret["Secrets Manager:<br/>github-webhook-secret"]
            PAT["Secrets Manager:<br/>github-pat"]
            WorkerIAM["IAM User:<br/>treadmill-&lt;id&gt;-worker"]
            BillingAlarm["CloudWatch:<br/>billing alarm $5/mo"]
        end
    end

    GitHub["GitHub"]

    CLI -->|cdk deploy --context deployment_id=&lt;id&gt;| CloudLite
    CLI -->|aws cloudformation describe-stacks| CloudLite
    CLI -->|writes resolved ARNs| YAML
    CLI -->|aws sts get-caller-identity assert<br/>Account == aws_account_id| AWS
    CLI -->|docker run with env from yaml| LocalContainers

    AWSProfile -.->|AWS_PROFILE| CLI
    YAML -.->|deployment_mode, ARNs, secret names| API
    YAML -.->|deployment_mode, ARNs| Worker

    GitHub -->|POST webhook| APIGW
    APIGW -->|invoke| WebhookLambda
    WebhookLambda -->|sqs.send_message| WebhookInbox
    API -->|sqs.receive_message long-poll| WebhookInbox
    API -->|secretsmanager.get-secret-value| WebhookSecret
    API -->|sns.publish step.*/workflow.*| EventsTopic
    EventsTopic -->|sqs.subscription| WorkQueue
    EventsTopic -->|sqs.subscription| CoordQueue
    Worker -->|sqs.receive_message claim| WorkQueue
    Worker -->|sns.publish step.completed| EventsTopic
    API -->|sqs.receive_message| CoordQueue

    Worker -.->|access keys via env| WorkerCreds
    WorkerIAM -.->|grants| WorkerCreds
```

## References

- ADR-0002 — deployment-mode taxonomy precedent.
- ADR-0007 — webhook endpoint canonicalization; superseded for `dev_local` by ADR-0017's AWS-side path.
- ADR-0011 — "simplest viable shape" posture for messaging.
- ADR-0017 — webhook-receiver internals (sibling decision).
- ADR-0019 — supersedes Q16.c's host-mount delivery mechanism.
