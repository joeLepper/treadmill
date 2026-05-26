# infra

## Purpose

This directory contains the Treadmill AWS CDK app, the single source of truth for infrastructure topology in both local (moto + Docker via the local adapter) and AWS (CloudFormation via cdk deploy) environments. The CDK app synthesizes provider-agnostic constructs into CloudFormation JSON, ensuring identical service definitions, scaling policies, IAM roles, secrets, and observability pipelines across dev and prod. It is the contract that binds the local adapter and the AWS deployment together.

## Key surfaces

- `treadmill_infra/app.py` — root CDK app; dispatches on `mode` + `deployment_id` + `include_observability` context flags to synthesize the right stacks.
- `treadmill_infra/stacks/cloud_lite.py` — `TreadmillCloudLite`: messaging (SNS/SQS), secrets, webhook receiver, billing alarm.
- `treadmill_infra/stacks/observability.py` — `TreadmillObservabilityStack`: EC2 + docker-compose Grafana/Tempo/Loki/Prometheus/OTel stack, one per deployment (ADR-0020). Synthesized alongside CloudLite when `--context include_observability=true`.
- `treadmill_infra/constructs/` — reusable CDK constructs: messaging (SNS/SQS), deploy_events (GitHub webhook receiver), secrets (AWS Secrets Manager), observability (billing alarm).
- `observability/` — docker-compose + config files for the Grafana ecosystem stack. Uploaded as an S3 asset by `TreadmillObservabilityStack`; the EC2 user-data downloads and runs it.
- `cdk.json` — CDK configuration; specifies context values, output paths, and synthesis target.

## Recent changes

- PR (this change) — `observability/dashboards/treadmill-overview.json` gained a "Claude token usage (ADR-0020)" row with four Prometheus panels backed by `treadmill_claude_tokens_{input,output,cache_creation,cache_read}_total` counters exported by the worker via OTel: stacked rate by type, totals by role, totals by model, and a `$task_id`-filtered drilldown by model. Spend-signal aggregations use `input + output` (cache_read is free, cache_creation paid-but-small).
- ADR-0055 step 4 — `SecretsConstruct` accepts `claude_account_names: list[str]` (passed from `cloud_lite.py` reading the `claude_accounts` CDK context flag as a comma-separated list). For each name, creates `treadmill-<deployment_id>/claude-account-<name>` (empty Secret; operator populates via `put-secret-value`), extends the API IAM user's `GetSecretValue` ARN list to include it, and emits a `ClaudeAccountSecret<Pascal>Name` CFN output whose value is the deterministic secret name. Synth: `cdk synth -c claude_accounts=personal,osmo`. Backward-compatible — empty/unset list ⇒ no extra resources.
- PR (this change) — Added `TreadmillObservabilityStack`: CDK stack that deploys Grafana + Tempo + Loki + Prometheus + OTel Collector on EC2 per ADR-0020. Added `observability/` compose dir. Extended `treadmill-local init` to merge observability CFN outputs and runtime.py to inject `OTEL_EXPORTER_OTLP_ENDPOINT`.
- [#34](https://github.com/anthropics/treadmill/pull/34) — treadmill-local init reads the new CFN output into the YAML per ADR-0016's deployment config schema.
- ADR-0030 plan landed — federated in-repo agent context (this AGENT.md is part of that backfill).
- [#19](https://github.com/anthropics/treadmill/pull/19) — Introduced host-side credential injection pattern for local development (ADR-0019).

## Pitfalls

- CDK does not validate CloudFormation condition names or resource properties at synth time; invalid names fail silently at deploy or adapter runtime. Always run `cdk synth` locally and validate the JSON before pushing.
- The local adapter reads CDK JSON output directly; changes to construct names or property keys will break the adapter's dispatch logic. Coordinate any refactors with `tools/local-adapter/` changes.
- SQS subscription filter policies are defined as CDK L1 constructs and are easy to misconfigure; test filter behavior against real SQS messages locally before deploying.
- IAM role statements are not validated for least-privilege at synth time; it is easy to over-grant permissions. Use AWS IAM Access Analyzer or manual review before committing broad-scoped roles.

## Navigation

- **Adjacent:** `tools/local-adapter/` (interprets this CDK output); `services/api/`, `workers/agent/` (deployed by this CDK app).
- **Decisions:** ADR-0002 (local-first via Treadmill-native CDK adapter); ADR-0007 (pre-prod environments per changeset); ADR-0016 (dev-local deployment topology); ADR-0019 (host-side credential injection).
- **Follow:** Start with ADR-0002 to understand the provider-agnosticism contract; read the stacks to see how services and backing services are wired.
