# Plan: Local adapter spike

- **Status:** completed
- **Date:** 2026-05-07
- **Related ADRs:** ADR-0002

## Goal

Validate that ADR-0002's approach — a Treadmill-native local adapter that interprets CDK synth output to provision moto-backed AWS-managed services and start ECS task definitions as native Docker containers — is feasible within three working days of focused work. The spike outcome decides whether ADR-0002 advances to `accepted` as drafted, gets amended to reflect what we learned, or triggers an abort to LocalStack Base.

## Success criteria

By end of day three:

1. A minimal CDK stack defines: an SNS FIFO topic, an SQS FIFO queue subscribed to it, an S3 bucket, and an ECS task definition for a "noop worker" that polls SQS, processes one message, and exits with `EXIT_AFTER_STEP=true`.
2. `treadmill local up` brings the stack up: moto provisions SNS / SQS / S3, the autoscaler is running, no worker is initially running, and `treadmill local status` shows the empty steady state.
3. Publishing one message via boto3 against `AWS_ENDPOINT_URL=moto` results in the autoscaler observing depth > 0, starting one worker container, the worker processing the message and exiting, and the autoscaler not starting a replacement.
4. Publishing three messages in quick succession results in three workers spawning sequentially over time (one at a time, since workers exit after one message), each processing one message, and after the idle window passes, zero workers running. `treadmill local logs` shows the activity.
5. `treadmill local down` cleanly tears everything down — no orphan containers, no leaked moto state on next `up`.
6. The same CDK stack passes `cdk synth` without modification and is structurally a valid stack for `cdk deploy` against real AWS — verified by inspection of the synthesized template, not by deploying.

## Constraints / scope

### In scope
- One CDK stack at minimum surface (SNS FIFO + SQS FIFO + S3 + ECS task def + `ApplicationAutoScaling::ScalingPolicy`).
- Adapter CLI: `treadmill local up`, `down`, `status`, `logs`.
- Naive target-tracking autoscaler: poll SQS depth on a fixed interval, reconcile worker count against the policy, start replacements as workers exit.
- Python everywhere — CDK in Python, adapter in Python, noop worker in Python. `uv` for package management.

### Out of scope
- The Treadmill API itself. We validate adapter primitives, not the full system.
- RDS / ElastiCache provisioning. We use raw Postgres and Redis containers if needed; not yet wired through CDK.
- Secrets Manager and SSM in moto.
- Mid-step worker termination (`StopTask`). Workers always run to completion.
- Step scaling, scheduled scaling, custom CloudWatch metrics. Target tracking on SQS depth only.
- More than one scalable target.
- OTEL collector wiring. Logging through container stdout is sufficient.
- Real-AWS deployment of the spike stack. We verify synth correctness by reading the template; we do not actually deploy.

### Budget
Three working days. If end of day three does not show success criteria 1–5 passing, we abort to LocalStack Base, mark this plan `abandoned`, and amend ADR-0002 to record the learning.

## Sequence of work

- **Day 1 — Bootstrap and provisioning.** Initialize the Treadmill repo's Python project (`uv init`), scaffold the CDK app, write the minimal stack. Stand up the adapter's `up` command to: shell out to `cdk synth`, parse the template, provision SNS / SQS / S3 in moto via boto3. Verify by inspecting moto state after `up`.
- **Day 2 — Worker round-trip.** Write the noop worker (poll SQS, log message, exit). Build its Docker image. Extend the adapter to: read the ECS task definition from CFN, start the container with correct env wiring (`AWS_ENDPOINT_URL`, IAM identity via `amazon-ecs-local-container-endpoints`). End-to-end: publish one message, see one worker process it and exit. `treadmill local down` works.
- **Day 3 — Autoscaling and polish.** Implement the target-tracking control loop. Validate the three-message scenario from success criterion 4. Add `treadmill local status` and `logs`. Inspect the synthesized template to confirm criterion 6.

## Diagram

The startup and autoscaling control-loop diagrams in ADR-0002 describe the intended end-state. The spike implements them at minimum scale.

## Risks / unknowns

- **CFN parsing complexity.** We do not yet know how heavy `ApplicationAutoScaling::ScalingPolicy` is to interpret. Mitigation: start the autoscaler with a hard-coded simple policy (min=0, max=3, target depth=1) on day 3; promote to CFN-driven only after the loop works.
- **moto fidelity for FIFO subscription filters.** Some SNS-to-SQS subscription behaviors may diverge from real AWS. Mitigation: keep the subscription unfiltered for the spike; revisit filtering when we add a second queue.
- **`ecs-local-container-endpoints` quirks.** The project is older and lightly maintained. Mitigation: budget half a day on day 2 specifically for wiring it up; if it blocks, swap for hand-rolled task-metadata env vars.
- **Python CDK construct gaps.** Some CDK constructs are richer in TypeScript than Python. Mitigation: if we hit a gap, drop to L1 (CFN-equivalent) constructs rather than abandon Python.
- **Scope drift to "make the spike production-ready."** We will resist. The spike validates feasibility; productionization is a separate plan.

## Decisions captured during execution

- **2026-05-07** Adopted uv workspace at the repo root with three members: `infra/`, `tools/local-adapter/`, `workers/noop/`. Editable installs require non-`src/` package layout; flattened accordingly.
- **2026-05-07** Pinned `motoserver/moto:5.0.28` and exposed it on host port 5001 to avoid common conflicts on 5000. Moto runs as a Docker container labeled `treadmill.managed=true`.
- **2026-05-07** Adapter shells out to the CDK CLI (`cdk synth`) rather than embedding CDK in-process. Keeps the adapter language-agnostic about how the CDK app is authored.
- **2026-05-07** Required upgrading the host CDK CLI from 2.1105 to 2.1121 (CFN schema mismatch with `aws-cdk-lib` 2.253). Resolved via `npm install -g aws-cdk@latest --registry=https://registry.npmjs.org/`. Flagged for an environment-pinning ADR if it bites again.
- **2026-05-07** Adapter's CFN reference resolver handles only `Ref`, simple `Fn::GetAtt`, and `Fn::Join` so far. Sufficient for SNS/SQS/S3/Subscription; will need extension for IAM and ECS task definitions on day 2.
- **2026-05-07** Day 1 shipped without tests. User caught the gap; we backfilled 20 tests (parser units, provisioner integration via in-process moto, CLI smoke, CDK assertions). Captured as `learning: features-ship-with-tests` with a proposed rule and hybrid remediation. Going forward, every Day's work lands with its tests in the same session.
- **2026-05-07** Day 2 ships with 8 new tests in the same session — runner unit tests covering env wiring, locally-augmented defaults, user-env precedence, unresolvable refs, single-container family enforcement. Total suite: 28 green.
- **2026-05-07** Day 2 chose to skip `amazon-ecs-local-container-endpoints` for now. Static fake credentials in container env (`AWS_ACCESS_KEY_ID=test` etc.) plus `AWS_ENDPOINT_URL` pointing at the moto container's network address are sufficient for moto, which accepts any credentials. Real-IAM behavior needs the sidecar; we add it when we add a workload that actually verifies IAM.
- **2026-05-07** Refactored CFN value resolution: `resolve_value()` is now a module-level function in `synth.py` shared by both `provisioner.py` and `runner.py`. Avoids two diverging implementations of `Ref` / `Fn::GetAtt` / `Fn::Join`.
- **2026-05-07** Worker-on-network reachability: workers run on the `treadmill-local` Docker network and reach moto via container DNS (`http://treadmill-local-moto:5000`), even though the queue URL embedded in the task-def env still points at the host-mapped `localhost:5001`. Modern boto3 honors `AWS_ENDPOINT_URL` regardless of the queue URL host, so the override works without rewriting URLs. If a future boto3 version stops honoring this, we'll rewrite at the runner.
- **2026-05-07** `start_worker_once()` is idempotent across CLI invocations: if state isn't in memory (separate process) it re-synths and re-provisions before resolving the spec. moto's create_topic / create_queue / create_bucket are idempotent, so noisy but safe.
- **2026-05-07** Day 3: Autoscaler implemented as a pure-logic class with injectable callables (`queue_depth_fn`, `worker_count_fn`, `start_worker_fn`), then wired in production via `main()` to a real boto3 SQS client + Docker SDK. Tests cover the tick logic without Docker or moto; production wiring is exercised by the end-to-end smoke.
- **2026-05-07** Day 3: Autoscaler runs as a detached subprocess (`Popen` with `start_new_session=True`), PID tracked at `.treadmill-local/autoscaler.pid`, log at `.treadmill-local/autoscaler.log`. `down` signals SIGTERM, waits up to 2s, escalates to SIGKILL if needed. Same lifecycle pattern would extend to other long-running adapter components.
- **2026-05-07** Day 3: Three-message scenario validated. depth=3 → tick spawns worker → worker processes one → exits → tick spawns next → ... → depth=0, no further spawns, total run 3 workers, all exit 0. Idle silence: autoscaler logs only when there's something to do.
- **2026-05-07** Day 3: Spike success criterion 6 confirmed by inspecting the synth output. 31 standard CFN resource types, no localhost references, no spike-only constructs. The same CDK is real-AWS-deployable.
- **2026-05-07** Day 3: Total test suite at end of spike: 40 passing across CFN parser, runner, provisioner (against in-process moto), autoscaler (with fakes), CLI smoke, and CDK assertions. Every Day's work landed with its tests in the same session.

## Post-mortem

### What worked

- **The single-source-of-truth claim held.** CDK in Python authored once, `cdk synth` interpreted by the adapter for local execution, `cdk deploy` would land the same stack in real AWS. No parallel topology definition existed at any point.
- **moto in daemon mode plus native Docker for our compute is the right shape.** Moto handled SNS / SQS FIFO / S3 / IAM / Subscriptions cleanly; Postgres / Redis / OTEL never came up because the spike didn't need them, and when they do they slot in as plain containers, not RDS / ElastiCache emulation.
- **`AWS_ENDPOINT_URL` plus container-network DNS** removed any need to rewrite queue URLs in env. boto3 honors the global endpoint override regardless of the embedded URL host. Less code, cleaner mental model.
- **Autoscaler as injectable callables** made the control loop trivial to unit-test without spinning up Docker. The same logic ships to production by wiring real callables in `main()`.
- **The ship-with-tests rule, applied from Day 2 onward.** Each day's code landed with its tests in the same session, total 40 green at spike close.

### What surprised us

- **CDK CLI / library version skew bit Day 1.** `aws-cdk-lib` 2.253 emits CFN schema 53; the local CLI was 2.1105 which only supports up to schema 50. We had to upgrade the CLI from public npm because the configured registry was a CodeArtifact one with an expired token. If we hit this twice, it's an ADR for environment pinning.
- **uv workspaces + hatchling + `src/` layout did not auto-wire editable installs.** Flat `treadmill_local/` works; `src/treadmill_local/` produced empty wheel records. Worth knowing for the next package we add.
- **pytest in a monorepo cannot have `__init__.py` in two sibling `tests/` directories** — they collide as the same package. Removing both made pytest use rootdir-relative naming and resolved the collision. Minor but easy to forget.
- **The autoscaler's "silent on idle" log convention surprised me as a reader at first.** Logging only on non-trivial ticks is the right choice (avoids spam) but obscures liveness during testing. We rely on the PID file for that signal.

### What should become an ADR, learning, or rule

- **Already captured:** `learning: features-ship-with-tests` (2026-05-07). Rule + remediation proposed; promote when the rule engine exists.
- **Candidate learning:** `cdk-cli-version-pinning` — the spike's first blocker was a tooling drift we did not anticipate. If we hit it again, this becomes an ADR (pin CDK CLI in a project file or via a wrapper script).
- **Candidate ADR:** `local-adapter: bounds and policy interpretation`. The spike implemented "track depth, clamp to [min,max]" as the local autoscaler policy — close to but not identical to the CDK step-scaling resource. Long-term we want the local interpretation to follow CDK exactly. Worth an ADR once we add a second policy shape.
- **No new rules** beyond the one already proposed.

### What this plan teaches us about future plans

- **3-day budget was right.** All success criteria met within the budget without overrun.
- **The "Decisions captured during execution" running log was load-bearing.** It accumulated 11 entries across 3 days; they form most of the post-mortem material above. Without it, post-mortem would have required reconstructing memory.
- **Out-of-scope was respected.** RDS / ElastiCache / Secrets Manager / SSM / step-scaling fidelity / `amazon-ecs-local-container-endpoints` / multi-stack apps / mid-step termination — none crept in. The discipline came from naming them in the plan, not from any tooling.
- **Future plans should default to this shape**: explicit out-of-scope, daily structure, running log, post-mortem schema.
