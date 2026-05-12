# ADR-0024: Local-mode auto-redeploy on merge to main

- **Status:** proposed
- **Date:** 2026-05-12
- **Related:** ADR-0017 (webhook ingestion), ADR-0018 (autoscaler — precedent for host-side supervisor processes), ADR-0021 (existing pr_merged trigger), ADR-0019 (host-side credential injection — same execution context)

## Context

Treadmill workers + API in `dev_local` mode run as Docker containers
on the operator's laptop. When a PR lands on `main` that touches
worker or API code, the running containers are still serving the
*previous* image — the autoscaler's pre-flight `_ensure_images_built`
only rebuilds when a fresh `up` happens, and the existing API
container survives across cycles. **Until the operator manually
`treadmill-local down + up`, the production-deployed-on-main code
is not what's actually running.**

GitHub Actions is the conventional answer to "deploy on merge," but
it can't push artifacts onto the operator's laptop — the laptop is
NAT'd, not addressable, not on a known schedule, and not running
anything that listens for incoming deploy requests. **The deploy
trigger has to flow from main → AWS → laptop, not the other way.**

This is operationally urgent because Treadmill is now building
Treadmill: today's o11y smoke chain (PR #9 onward) produces PRs that
themselves modify worker / API / local-adapter code. Each merged PR
needs to land on the running stack before the next downstream task
fires. Without auto-redeploy, the chain breaks: task N's PR merges,
task N+1's wf-author worker spawns against the *old* worker image,
the new code that task N landed isn't visible to task N+1.

The mechanism already exists at the messaging layer: ADR-0017's
webhook ingestion delivers `pr_merged` events to the events SNS
topic. ADR-0021 already adds a handler for the plan-doc subset of
those events. We need a sibling handler for the code-change subset
that knows how to rebuild + cycle containers on the laptop.

## Decision

### New host-side subprocess: `treadmill-local deploy-watcher`

`treadmill-local up --deployment <id>` spawns a new long-running
subprocess (sibling to the autoscaler, per ADR-0018's precedent).
The deploy-watcher:

1. **Subscribes to a new SQS queue** dedicated to deploy events.
2. **Polls + dequeues** `pr_merged` events filtered for code-touching
   PRs.
3. **Fetches the merged PR's changed files** via the gh API (reusing
   the GITHUB_TOKEN env var per ADR-0019's injection — once ADR-0023
   lands, it uses the API IAM user's gh access too).
4. **Categorizes** the changes against a small dispatch table.
5. **Acts** per category: rebuild + cycle the relevant container, or
   notify the operator if auto-action is unsafe.

It runs on the operator's host (not in a container) so it can shell
out to `docker build` + `docker restart` against the local Docker
daemon. Same execution context as the autoscaler.

### New SQS queue + SNS subscription

A new SQS queue: `treadmill-<deployment_id>-deploy-events`. Provisioned
by the CDK (extends `WebhookReceiverConstruct` or sibling). Subscribed
to the existing events SNS topic with a message-attribute filter
limiting it to `entity_type=github, action=pr_merged` (so the watcher
doesn't see plan / task / step lifecycle events it doesn't care
about).

Separate queue (not the existing coordination queue) because:
- Different consumer (host-side watcher vs API container).
- Different failure semantics (a rebuild failure isn't a consumer
  failure; can DLQ differently).
- Operator can flush the deploy-events queue without affecting the
  consumer's event projection.

### Dispatch table

Each changed file is matched against ordered globs; the first match
wins:

| Glob | Action | Notes |
|---|---|---|
| `services/api/**` | rebuild `treadmill-api:dev`, restart `treadmill-api` container | API is a long-running container; restart cycles it |
| `workers/agent/**` | rebuild `treadmill-agent:dev` (no restart needed) | Workers are one-shot; next autoscaler spawn picks up new image |
| `infra/**` | **notify only** — print operator-visible message naming the changed files; do NOT auto-run `cdk deploy` | Infra changes affect AWS resources; auto-deploying them is too high-blast-radius for v0 |
| `tools/local-adapter/**` | **notify only** — print operator-visible message | The watcher is itself part of the local-adapter; it can't rebuild itself live (it would die mid-restart). Operator runs `treadmill-local down + up` manually |
| `docs/**`, `cli/**`, `.claude/**`, `*.md`, etc. | no-op | Documentation + CLI changes don't affect running services |

Mid-rebuild failures (build error, container restart fails): the
watcher logs the error at the deployment-events stream, DOES NOT
re-queue (a rebuild that fails the first time will fail the second
time the same way — the operator needs to investigate). Future:
classify build errors to decide retry vs DLQ. v0: always DLQ.

### Idempotency

Each `pr_merged` event carries the merge commit SHA. The watcher
tracks the last-successfully-applied SHA per (deployment_id,
category) tuple in a small state file
(`.treadmill-local/deploy-watcher-state.json`). If an event arrives
for a SHA that's already been applied (re-delivery, SNS replay,
operator forced a cycle), the watcher logs + acks without rebuilding.

### `--no-deploy-watcher` opt-out

Same shape as ADR-0018's `--no-autoscaler` flag. The operator can
disable the watcher when debugging or running an intentional
old-version session. Default-on in dev_local mode.

### Composability with the other `pr_merged` triggers

ADR-0021 (plan_doc_merged) and the forthcoming ADR-merge → wf-plan
trigger consume the same upstream event (`pr_merged`) but with
different file-path filters and different actions. At three triggers
the architectural question of "registry pattern vs dedicated
handlers" becomes worth asking; this ADR explicitly **defers that
question** to ADR-0026 (or whatever) when the third trigger lands.
For v0:

- **ADR-0021**: in-API handler in `coordination/plan_doc_trigger.py`,
  consumes `coordination` queue events (the consumer's projection).
- **This ADR**: host-side watcher subprocess, consumes the new
  `deploy-events` queue (a parallel SNS subscription).
- **Future ADR-merge → wf-plan**: in-API handler in
  `coordination/adr_doc_trigger.py`, consumes the same `coordination`
  queue (sibling to plan-doc trigger).

The trade-off: two handlers live in the API (plan + adr), one lives
on the host (deploy). That's the natural seam — the API-side ones
spawn work for Treadmill to do; the host-side one operates on
Docker. Splitting them by execution context is honest.

When a fourth or fifth pr_merged trigger appears with a meaningfully
different action, we'll consolidate behind a registry. Until then,
three small siblings.

### What this does NOT do

- Does NOT cover `fully_remote` deployment. When the API runs as an
  ECS task in AWS, code-merge → redeploy happens via `cdk deploy` or
  ECS task definition updates triggered by a CI pipeline. Out of
  scope here; the future `TreadmillCloudFull` ADR makes that call.
- Does NOT auto-deploy infra changes. Operator-mediated only.
- Does NOT rebuild the deploy-watcher itself when local-adapter code
  changes — it can't (mid-restart die). Operator-cycled.
- Does NOT verify the rebuild succeeded by running tests. The
  rebuild succeeding == `docker build` returning 0. Test correctness
  is the merging-operator's responsibility (or wf-review's, before
  merge).

## Bunkhouse precedent

Bunkhouse's deploy model is fully cloud-native: GitHub Actions runs
`cdk deploy` (or `aws ecs update-service`) on each merge to `main`,
which rolls the ECS service. No laptop-side step exists because
bunkhouse's compute is all in AWS. The Treadmill auto-redeploy
mechanism is a *new* concept (no bunkhouse equivalent) specific to
the local-mode topology — bunkhouse didn't have this need.

For Treadmill's eventual `fully_remote` mode, we'll likely adopt
bunkhouse's GitHub-Actions-mediated approach. This ADR is dev-local-
scoped.

## Trade-offs

- **One more host-side subprocess.** Adds operator-visible complexity
  (PID files, log files, lifecycle management). Mitigated by reusing
  the autoscaler's existing supervisor patterns; the watcher's
  shape is "a sibling autoscaler."
- **New AWS resources per deployment.** One SQS queue, one SNS
  subscription, one DLQ. ~$0 at this volume.
- **Latency**: the watcher polls SQS on a tick (10s default). A merge
  takes 10-30s to land in containers. Acceptable for dev velocity;
  operator can `treadmill-local up` explicitly for urgent cases.
- **Rebuild ≠ verified.** The watcher confirms the build succeeded
  but doesn't run tests. Test correctness flows through wf-review
  (pre-merge). If a merge lands code that builds but doesn't work,
  the watcher will silently install it — the next worker run will
  surface the breakage.
- **State file lives on the laptop.** The watcher's idempotency state
  (`deploy-watcher-state.json`) is laptop-local. If the operator
  moves the dev environment between machines (rare), the watcher
  may re-apply already-applied SHAs (harmless — rebuild is
  idempotent).
- **The local-adapter-changes case is operator-mediated.** Self-
  modification's chicken-and-egg is real; we don't try to solve it.

## Alternatives considered

- **GitHub Actions deploys to the laptop via SSH/Tailscale.** GH
  Actions could connect to the operator's machine over Tailscale (or
  similar VPN) and run `docker build` remotely. Requires Tailscale
  setup; the laptop must be online + reachable when the workflow
  runs. Rejected: too much standing infrastructure (Tailscale
  account, exit node, secrets in GH Actions). The watcher-pulls-
  events pattern is simpler — laptop is always the actor.
- **Polling: the watcher periodically checks `git fetch
  origin/main` and rebuilds on new commits.** Doesn't require AWS
  resources; the watcher just polls GitHub. Loses precision (no
  per-file dispatch table without parsing the commit's changed
  files) and pulls fresh blobs from GitHub every tick. Rejected
  vs SNS-fanout: pull-vs-push semantics make the push approach
  cheaper + faster.
- **Combine the watcher with the autoscaler.** One subprocess does
  both jobs. Doable; saves one PID-file. Rejected: different concerns
  (queue-depth-driven vs event-driven); bundling obscures both.
- **Webhook into the laptop directly (skip the SQS queue).** GitHub
  webhook → API Gateway → Lambda → operator-side endpoint via
  ngrok/Tailscale. Adds the operator-online-NAT problem the ADR
  explicitly rejects elsewhere.
- **Skip auto-deploy; document the operator-cycle requirement.**
  Operator runs `treadmill-local down + up` after every merge to
  main. Real-world: operators forget; the chain breaks silently.
  Rejected at v0 because the chain is now an operational concern
  (Treadmill builds Treadmill).
- **Defer to a generic pr_merged trigger registry now.** Build the
  registry upfront for plan + adr + code triggers. Premature: only
  one trigger exists today (plan), this ADR adds the second, the
  third is forthcoming. Three is the right time to consolidate. Two
  is too early.

## Open questions

- **Q24.a — How does the watcher signal failures?** A failed rebuild
  is operator-visible (printed to the deploy-watcher log), but no
  active push: the operator has to *look*. Should the watcher post
  a comment on the merged PR ("auto-rebuild failed: <error>")?
  That'd close the operator-attention loop. Defer until the first
  time a silent watch-failure bites.
- **Q24.b — Should the watcher emit observability events?**
  Per-rebuild duration, success/failure counts → ADR-0020's
  Grafana stack. Naturally fits as Prometheus metrics. Bank for
  ADR-0020's implementation.
- **Q24.c — What about partial PRs touching multiple categories?**
  A single PR can touch `services/api/` AND `workers/agent/`. The
  watcher rebuilds both. What if one rebuild succeeds and the other
  fails? Today's dispatch table is per-file; the watcher accumulates
  by category and rebuilds at category granularity. Failure of one
  category doesn't roll back the other (no transactional contract).
- **Q24.d — `tools/local-adapter/` changes triggering "operator
  please cycle" notifications: how do those reach the operator?**
  Today the watcher logs to a file the operator may not be watching.
  Could emit a `notify-send` desktop notification on Linux; could
  post a PR comment; could write to a small status surface the
  operator polls. Defer.
- **Q24.e — Does the watcher subscribe to its own deploy events?**
  i.e., when the deploy-watcher's own code changes (in
  `tools/local-adapter/`), it gets a notification it can't act on.
  Today's dispatch table handles this with "notify only"; the
  operator restarts manually. Could be improved with a self-update
  protocol (watcher writes "needs restart" file → operator's next
  `treadmill-local status` reports it). Defer.

## Consequences

- **CDK**: new SQS queue + SNS subscription resources, likely
  extending `WebhookReceiverConstruct` or as a sibling `DeployEventsConstruct`.
  New CFN outputs: `DeployEventsQueueUrl`, `DeployEventsDlqUrl`.
- **`treadmill-local init`**: pulls the new CFN outputs into the
  per-deployment YAML under `aws.deploy_events_queue_url` and
  `aws.deploy_events_dlq_url`.
- **`tools/local-adapter/`**: new module `deploy_watcher.py` (sibling
  to `autoscaler.py`). New CLI flag `--no-deploy-watcher` on `up`.
  Lifecycle (PID file, SIGTERM, log file) mirrors autoscaler.
- **State file**: `.treadmill-local/deploy-watcher-state.json` tracks
  last-applied SHA per category.
- **gh API access**: the watcher uses the existing GITHUB_TOKEN env
  (per ADR-0019's host-side injection) to fetch a PR's changed
  files.
- **Composes with ADR-0023** (API IAM creds): the watcher's SQS
  receive happens on the *host*, not in a container — it uses the
  operator's SSO via `AWS_PROFILE` like the autoscaler does. When
  SSO expires, the watcher fails the same way; operator runs
  `aws sso login` to recover. No special handling.
- **Composes with ADR-0021** (plan_doc trigger): both consume
  `pr_merged` events but from different queues. No cross-contention.
- **Composes with the future ADR-merge → wf-plan trigger**: same
  pattern — different filter, different action, possibly different
  queue. When a fourth trigger appears, consolidate behind a
  registry.
- **Phase 2 self-driving criterion**: with this lands, the chain of
  PRs that the o11y plan produces can be merged in sequence without
  the operator needing to cycle the stack between merges. Treadmill
  builds Treadmill, hands-off.
