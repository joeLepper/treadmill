# tools/local-adapter

## Purpose

This directory contains the Treadmill-native local adapter, the bridge between CDK synth output and a moto + native Docker substrate for local-first development. It reads the CloudFormation JSON synthesized by the CDK app and operationalizes it by provisioning AWS-managed primitives (SNS, SQS, S3, IAM, Secrets, SSM) against moto, launching Postgres and Redis as native containers, wiring ECS task definitions to docker run commands, and running an autoscaling control loop that matches SQS queue depth to container count. The adapter ensures local behavior is faithful to AWS production for the cases developers care about: service definitions, scaling policies, credential injection, and the exit-then-restart cycle.

## Key surfaces

- `treadmill_local/cli.py` — entry point; `treadmill-local up` brings the substrate online, `down` tears it down, `logs` streams container output.
- `treadmill_local/runtime.py` — core orchestrator; loads CDK JSON, dispatches to provisioners, starts containers, runs the autoscaler loop.
- `treadmill_local/provisioner.py` — provisions AWS primitives (SNS, SQS, S3, IAM, Secrets, SSM) into moto.
- `treadmill_local/autoscaler.py` — target-tracking control loop; reads SQS depth from moto, docker stats from Docker daemon, computes desired worker count from scaling policy, launches/drains containers.
- `treadmill_local/deployment_config.py` — writes `~/.treadmill/<deployment_id>.yaml` with container ports and endpoint URLs per ADR-0016's schema.
- `treadmill_local/subprocess_logging.py` — bounded logging helpers for the autoscaler + deploy-watcher subprocesses: a `configure_rotating_logging` setup (size-rotating file handler replacing stdout) and a `RateLimitedErrorLogger` that logs the first occurrence of an error in full and collapses repeats into periodic counted summaries.

## Recent changes

- PR (this change) — Autoscaler and deploy-watcher subprocesses now own their own log files via `subprocess_logging.configure_rotating_logging` (10 MB × 3 backup cap). Parent spawn sites in `runtime.py` redirect `stdout`/`stderr` to `DEVNULL` and pass the log path through `TREADMILL_AUTOSCALER_LOG_FILE` / `TREADMILL_DEPLOY_WATCHER_LOG_FILE`, replacing the unbounded `open(<file>, "ab")` redirect that filled a developer's disk on 2026-05-20. Persistent loop errors now log a full traceback once per signature and then collapse repeats into periodic counted summaries via `RateLimitedErrorLogger`, so a wedged credential or unreachable queue no longer dumps a fresh stack trace every iteration. Scheduler spawn untouched.
- PR (prior change) — Extended `treadmill-local init` to also try reading from `TreadmillObservabilityStack` CFN outputs (merged when deployed; gracefully skipped when absent). Extended `_dev_local_api_env` + `_dev_local_worker_env` in `runtime.py` to inject `OTEL_EXPORTER_OTLP_ENDPOINT` from `aws.observability_collector_endpoint` per ADR-0020.
- [#36](https://github.com/anthropics/treadmill/pull/36) — Fetches API credentials at startup and injects them into the agent container environment.
- [#34](https://github.com/anthropics/treadmill/pull/34) — Reads CDK synth output into the deployment config YAML so containers can discover each other.
- [#2](https://github.com/anthropics/treadmill/pull/2) — Initial spike: moto + Docker Compose proof-of-concept (now evolved to docker run + autoscaler).

## Pitfalls

- Moto does not implement all AWS service behaviors; changes to the API's SQS or SNS usage can fail locally but succeed against real AWS. Always validate changes against a real AWS environment before moving to production.
- Docker resource limits (memory, CPU) in adapter container specs are hints, not enforced; if containers exceed their limits locally, Docker may kill them without warning. Use `docker stats` to monitor and test load locally before deploying.
- The autoscaler reads moto's SQS state directly; if moto's SQS implementation diverges from AWS behavior (message visibility, batch operations, long polling), the autoscaler may misbehave. Monitor the adapter's logs for scale-up/down decisions during development.
- Host-side credential injection via `startup_auth.py` happens at adapter startup; if credentials rotate or become invalid mid-session, the running containers will not refresh them until the next `treadmill-local up`.

## Navigation

- **Adjacent:** `infra/` (reads CDK synth output from this app); `services/api/`, `workers/agent/` (run as containers orchestrated by this adapter).
- **Decisions:** ADR-0002 (local-first + CDK as single source of truth); ADR-0016 (dev-local deployment topology); ADR-0018 (autoscaler in dev-local mode); ADR-0019 (host-side credential injection).
- **Follow:** Start with ADR-0002 to understand why this adapter exists; read `runtime.py` and `autoscaler.py` to understand the startup and scaling flow.
