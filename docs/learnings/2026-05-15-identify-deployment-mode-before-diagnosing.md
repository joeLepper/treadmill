---
date: 2026-05-15
trigger: pattern
status: captured
related: ADR-0024, ADR-0036, ADR-0037
last_crystallization_check: 2026-05-17
crystallization_backoff_until: 2026-05-24
crystallization_target: pending-second-instance
---

# Learning: Identify deployment mode before starting incident diagnosis

## Trigger

During the 2026-05-15 hands-free convergence session, tasks appeared stuck or
in unexpected states. Investigation efforts were misdirected at the wrong
observability surface — checking cloud-side metrics (SQS depth, CloudWatch
logs) while the system was running in dev-local mode (Docker containers, local
adapter). The correct signals (docker logs, local Postgres state) were not
consulted until the mode was confirmed. Time spent on the wrong surface
produced misleading results.

## Observation

Treadmill has two deployment modes with entirely different observability surfaces:

- **dev-local**: workers are Docker containers started by the local adapter;
  logs appear on stdout / `docker logs`; state is in a local Postgres container;
  there is no SQS, no CloudWatch, no ECS. ADR-0024 governs local auto-redeploy.
- **production (ECS)**: workers are ephemeral ECS tasks; logs are in CloudWatch;
  state is in RDS; queue depth is in SQS / Grafana dashboards.

Starting a diagnosis without confirming the active mode wastes time because
the signals are orthogonal. An ECS metric showing "no running tasks" is
expected behavior in dev-local. A `docker ps` showing no worker containers is
the alarming signal in dev-local. Consulting the wrong surface produces answers
that are technically correct for the wrong mode and misleading for the active one.

## Generalization

Before diagnosing any Treadmill incident — stuck tasks, missing PRs, silent
failures, unexpected state — the first step is always to confirm which
deployment mode is active:

1. Check `TREADMILL_DEPLOYMENT_MODE` in the local adapter config or env.
2. `docker ps | grep treadmill` — presence confirms dev-local workers are up.
3. `treadmill status` (when available) — reports mode + connected components.

Once mode is confirmed, use the appropriate diagnosis path. Do not mix
observability surfaces across modes.

## Proposed rule

Operational guidance rather than an enforceable code rule. The check is not
mechanical enough for a deterministic script, and the trigger (diagnosis
process) does not appear in a diff. Fits better in a runbook or AGENT.md
Pitfalls than in a rule YAML.

## Proposed remediation

Add "confirm deployment mode" as step 0 in any incident diagnosis runbook.
Until a runbook section exists, capture this in `docs/AGENT.md` Pitfalls so
the next investigator reads it before spinning up a diagnosis.

## Notes

The mistake is a context-switching error: the operator mentally switched to
"is something broken in production?" before confirming which environment was
active. The confirmation step is cheap (one command) and eliminates an entire
class of misleading signals. The cost of skipping it was several minutes of
investigation on the wrong surface.
