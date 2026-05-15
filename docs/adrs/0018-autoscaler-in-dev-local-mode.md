# ADR-0018: Autoscaler in dev-local mode

- **Status:** proposed
- **Date:** 2026-05-12
- **Related:** ADR-0002, ADR-0015, ADR-0016, ADR-0017

## Context

After ADR-0016's dev-local topology landed and the first end-to-end smoke succeeded, the gap between fully-local mode and dev-local mode became operator-visible:

- **Fully-local mode** (moto + local API + local workers): the local-adapter launches the autoscaler as a subprocess of `treadmill-local up`. The autoscaler watches the moto SQS queue depth and spawns one-shot worker containers (`EXIT_AFTER_STEP=true`) as messages arrive. The operator submits a plan and walks away — workflows chain through to completion without further intervention.
- **Dev-local mode** (real AWS messaging + local compute): the local-adapter explicitly *does not* start an autoscaler. D.2's report (per the Week-4 plan) called this out: *"autoscaler-in-dev-local is a separate decision."* The operator must `treadmill-local run-worker treadmill-agent --deployment personal` once per work-queue message. After a `treadmill submit`, that means: one invocation for wf-author, then another after the resulting PR triggers wf-review, etc. Every workflow step is a manual prompt.

The first smoke proved end-to-end works; it also proved the manual-invocation cost. The wf-review run from the smoke sat in the queue until the operator fired a second worker. That cost is fine once. As Treadmill moves toward "build Treadmill with Treadmill" (operator's stated direction), the autoscaler becomes the keystone — without it, every closed loop in the system requires a human cursor.

The fully-local autoscaler's `Autoscaler` class is already test-driven and dependency-injected (per `tools/local-adapter/treadmill_local/autoscaler.py`). The shape works against real AWS SQS too — boto3 is region-aware and the depth-polling API is identical. What's missing is the *wiring*: where to source the min/max bounds, which queue URL + family to watch, and how the autoscaler subprocess gets AWS credentials.

## Decision

### Wire the existing `Autoscaler` class for dev-local mode

No new autoscaler code. The class is correct. We add new *wiring* in `LocalRuntime._up_dev_local` (and parallel cleanup in `down`) that:

1. Reads autoscaler config from the deployment YAML.
2. Spawns the autoscaler subprocess with env pointing at the real AWS work queue.
3. Tears it down on `treadmill-local down --deployment <id>`.

### Config source: deployment YAML

The YAML schema (per ADR-0016) gains an `autoscaler:` block at the top level:

```yaml
deployment_id: personal
deployment_mode: dev_local
# ... existing fields ...
autoscaler:
  min: 0
  max: 2
  tick_seconds: 5
```

Defaults when the block is absent: `min=0`, `max=1`, `tick_seconds=5`. These match a "one worker at a time, scale to zero when idle" policy — which is also what a single-operator personal deployment wants. The 5-second tick (vs fully-local's 2s) accepts a little extra latency in exchange for lower AWS SQS API call volume against real AWS billing.

The `min=0` default matters: when the queue is empty there are zero idle workers, costing zero compute. The "spin up on demand" pattern is the entire point — long-lived workers waste laptop CPU + the operator's Claude credit-tier on idle long-polls.

### One autoscaler per deployment, one family at v0

Dev-local has exactly one task family (`treadmill-agent`). The autoscaler watches one queue (`aws.work_queue_url` from the YAML). No multi-family logic.

When dev-local grows multi-family (e.g., separate workers for wf-plan vs wf-author after the Phase E.3 intent-only-submit gap closes), this ADR will get amended; the existing `Autoscaler` already supports per-family instances.

### Subprocess + PID-file lifecycle (same as fully-local)

Reuse `STATE_DIR/autoscaler.pid` + `autoscaler.log` semantics. Deploy-specific naming would only matter if we ran multiple deployments concurrently from one laptop, which v0 doesn't. When multi-deployment lands, the PID-file path becomes deployment-suffixed.

`treadmill-local down --deployment <id>` SIGTERMs the autoscaler before removing the containers.

### Hard dependency: host-side credential injection (ADR-pending)

**Implementation must wait for the worker-credential redesign tracked in task #97.** Today's worker auth path is: mount `~/.aws` into the container → bootstrap-session uses SSO to fetch the long-lived IAM keys → worker-session uses the keys. That path fails for any worker spawned more than ~1h after the operator's last `aws sso login`:

- `:ro` mount: SSO token refresh writes fail with `Read-only file system`.
- `:rw` mount: container's root-uid leaves root-owned files in the host SSO cache, breaking the operator's own `aws sso login`.

An autoscaler in dev-local mode amplifies this. Manual `run-worker` fires once per step — the operator notices the failure and re-`sso login`s. An autoscaler fires every time a message arrives — the failure mode becomes silent: the operator submits, walks away, comes back to a half-failed pipeline.

The dependency: the autoscaler can only ship after dev-local workers stop depending on container-side SSO. The expected design (per task #97): local-adapter fetches the `worker-aws-credentials` secret value on the host once at startup, injects as `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` env vars on every spawned worker container. Worker `startup_auth.py` drops the bootstrap-session pattern; the env vars are present from t=0. No `~/.aws` mount on the worker at all.

This implementation order is **load-bearing**: shipping the autoscaler against today's worker-auth path produces a regression.

### Autoscaler's own AWS credentials

The autoscaler subprocess runs on the operator's host (not in a container) and calls SQS `GetQueueAttributes`. It uses the operator's SSO via `AWS_PROFILE=treadmill-personal` from the deployment YAML. The autoscaler subprocess inherits the parent shell's env, which is where `AWS_PROFILE` lives.

SSO expiry will eventually break autoscaler-side SQS polls too, but here the failure mode is benign: the operator notices "workers not spawning," runs `aws sso login`, and the autoscaler resumes on the next tick. Unlike worker SSO inside a container, the autoscaler can write to the host cache freely — it IS the host's child process.

Future hardening: catch the SSO-expired exception explicitly in `Autoscaler.run` and emit a clear stderr message ("operator: run `aws sso login --profile <profile>`"). Out of scope for v0.

### Manual override stays

`treadmill-local run-worker treadmill-agent --deployment personal` keeps working even when the autoscaler is running — they coordinate via the queue. The operator can fire a manual worker for debugging without disabling the autoscaler. The autoscaler sees the now-running worker via the docker label query and doesn't over-spawn.

A `--no-autoscaler` flag on `up` lets the operator suppress the autoscaler entirely (e.g., when debugging a specific worker failure in isolation).

## Trade-offs

- **Implementation depends on task #97** — autoscaler ships AFTER worker-credential redesign, not in parallel. The dependency is real (silent failure mode under SSO expiry); ignoring it would create a worse experience than not having the autoscaler at all.
- **Cost is operator-side compute, not AWS** — each spawned worker is a docker container on the laptop, not an ECS task. At `max=2` two workers running concurrently is fine on any modern machine. The cost surface that matters is **Claude API spend** (each worker run consumes credits); the autoscaler can't bound that automatically, so the `max` cap is the operator's lever.
- **Polling on real AWS adds API calls** — at 5-second ticks, `GetQueueAttributes` runs 12/min = 17k/month. Free under SQS's first-million-per-month tier. Bounded.
- **One scaler per deployment** — when an operator runs two deployments concurrently (personal + employer), they'll have two PID-files + two scalers. Today's PID-file path doesn't include `deployment_id`, so this is a follow-up. Single-deployment v0 doesn't hit it.
- **No down-scaling logic to write** — workers are one-shot (`EXIT_AFTER_STEP=true`), so "scale down" is just "stop spawning replacements." The existing Autoscaler does this by construction.

## Alternatives considered

- **Spin up the worker on-demand from the API itself** — when the API persists a `step.ready` event, it could shell out to `docker run treadmill-agent:dev`. Tightly couples the API to the operator's docker socket; works only when the API runs on the same machine as docker (which is always the case in dev-local, but doesn't generalize). Rejected: spawn-from-API breaks the cloud-portable shape ADR-0011 assumes.
- **Keep manual `run-worker` and let the operator script it** — e.g., a shell loop polling SQS depth + running `run-worker` when non-zero. That's what the autoscaler *is*. Codifying it in the local-adapter avoids per-operator shell-script drift.
- **Run a long-lived worker that drains the queue continuously** — drop `EXIT_AFTER_STEP=true`, let workers consume messages indefinitely. Two costs: workers don't have a clean recovery path on bugs (a hung worker holds the queue), and the worker container can't pick up new code without a restart. Rejected: one-shot workers are how ECS production runs them; keeping the same pattern locally is the right precedent.
- **Use AWS Application Auto Scaling against an EKS task definition** — would require the worker to live in AWS, contradicting ADR-0016's "compute is local" decision. Out of scope.

## Open questions

- **Q18.a — Should the autoscaler also restart the API on health-probe failure?** The API can crash (auto-migrate race, exhausted Redis pool, etc.). Today the operator manually `docker start treadmill-api`. The autoscaler is a long-running supervisor process already; adding API-restart logic is small. Defer until the API actually crashes outside this Week's session.
- **Q18.b — Should the autoscaler emit observability events back to Treadmill itself?** Each tick produces an `AutoscalerTick` dataclass. Persisting them as event rows would let the operator query "how many workers ran this hour?" — useful for cost analysis. Adds DB write load. Defer.
- **Q18.c — Concurrency on the FIFO work queue: how does `MaxCapacity > 1` interact with `MessageGroupId`?** SQS FIFO serializes consumers by message group. If all messages share one group, only one worker can be active at a time regardless of `max`. If groups are per-task, multiple workers can drain in parallel. The current dispatcher's `MessageGroupId` choice determines whether `max > 1` is effective. Verify before committing to a `max > 1` default.
- **Q18.d — Should the YAML default `max` be 1 or 2?** v0 ships `max=1` as the safe default (no concurrency surprises). Operators who want more bump it explicitly. Easy to relax later if usage shows it's safe.

## Consequences

- A new `autoscaler` block lives in `~/.treadmill/<deployment_id>.yaml`. The `treadmill-local init` command stamps defaults; the operator can edit by hand.
- `LocalRuntime._up_dev_local` gains autoscaler launch + tear-down (mirroring fully-local). `_up_dev_local` skipping the autoscaler today is the *only* behavioral gap to close.
- The `Autoscaler` subprocess inherits `AWS_PROFILE` + `AWS_DEFAULT_REGION` from the operator's shell. Setting them correctly is part of the `up` precondition (already true).
- This ADR's implementation is **blocked on task #97**. Until host-side credential injection lands, autoscaler-in-dev-local would silently break workers after the first hour. The Week-5 plan should sequence #97 then #92 (this ADR's implementation).
- Phase 2 success criteria 4 + 5 ("end-to-end PRs" + "every workflow firing") move from manually-demonstrable to demonstrably-automated once this lands.

## Diagram

```mermaid
sequenceDiagram
    actor Operator as Operator
    participant CLI as treadmill-local CLI
    participant Runtime as LocalRuntime._up_dev_local
    participant YAML as ~/.treadmill/&lt;id&gt;.yaml
    participant Autoscaler as Autoscaler subprocess
    participant WorkQueue as SQS work queue (real AWS)
    participant Docker as Docker daemon (host)
    participant Worker as treadmill-agent container

    Operator->>CLI: treadmill-local up --deployment &lt;id&gt;
    CLI->>Runtime: spawn dev-local
    Runtime->>YAML: read autoscaler.{min,max,tick_seconds}<br/>+ aws.work_queue_url
    YAML-->>Runtime: config
    Runtime->>Autoscaler: spawn subprocess<br/>(inherits AWS_PROFILE, AWS_DEFAULT_REGION,<br/>injected AWS keys per ADR-0019)
    Runtime->>Operator: PID written to STATE_DIR/autoscaler.pid

    loop every tick_seconds (default 5s)
        Autoscaler->>WorkQueue: sqs.GetQueueAttributes<br/>ApproximateNumberOfMessages
        WorkQueue-->>Autoscaler: depth
        Autoscaler->>Docker: docker ps --filter label=treadmill-family=treadmill-agent
        Docker-->>Autoscaler: running worker count
        alt depth &gt; 0 AND running &lt; max
            Autoscaler->>Docker: docker run treadmill-agent<br/>EXIT_AFTER_STEP=true
            Docker->>Worker: start
            Worker->>WorkQueue: sqs.receive_message (claim)
            WorkQueue-->>Worker: step message
            Worker->>Worker: execute step
            Worker-->>Docker: exit 0
        else depth == 0 OR running &gt;= max
            Autoscaler->>Autoscaler: no-op (scale-down by attrition)
        end
    end

    Operator->>CLI: treadmill-local down --deployment &lt;id&gt;
    CLI->>Autoscaler: SIGTERM (via PID file)
    Autoscaler-->>CLI: exit
    CLI->>Docker: stop containers
```

## References

- ADR-0002 — local-adapter subprocess lifecycle precedent.
- ADR-0015 — multi-step workflows; the autoscaler is what chains them without human cursor.
- ADR-0016 — dev-local topology; this ADR fills the autoscaler gap D.2 named.
- ADR-0017 — webhook ingestion that drives messages onto the work queue.
- ADR-0019 — host-side credential injection; load-bearing prerequisite (task #97 before task #92).
