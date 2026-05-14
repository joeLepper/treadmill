# infra

## Purpose

This directory contains the Treadmill AWS CDK app, the single source of truth for infrastructure topology in both local (moto + Docker via the local adapter) and AWS (CloudFormation via cdk deploy) environments. The CDK app synthesizes provider-agnostic constructs into CloudFormation JSON, ensuring identical service definitions, scaling policies, IAM roles, secrets, and observability pipelines across dev and prod. It is the contract that binds the local adapter and the AWS deployment together.

## Key surfaces

- `treadmill_infra/app.py` — root CDK app; orchestrates service and backing-service stacks, applies cross-cutting concerns (observability, networking).
- `treadmill_infra/stacks/cloud_lite.py` — stacks for local and AWS targets; defines ECS task definitions, SQS queues with subscriptions, SNS topics, S3 buckets, Postgres RDS, Redis ElastiCache, monitoring.
- `treadmill_infra/constructs/` — reusable CDK constructs: messaging (SNS/SQS), deploy_events (GitHub webhook receiver), secrets (AWS Secrets Manager), observability (OpenTelemetry + Grafana).
- `cdk.json` — CDK configuration; specifies context values, output paths, and synthesis target.

## Recent changes

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
