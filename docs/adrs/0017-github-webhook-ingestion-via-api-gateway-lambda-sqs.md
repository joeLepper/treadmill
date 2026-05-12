# ADR-0017: GitHub webhook ingestion via API Gateway + Lambda + SQS

- **Status:** accepted
- **Date:** 2026-05-12
- **Related:** ADR-0007, ADR-0011, ADR-0016

## Context

ADR-0016 commits Treadmill to a "dev-local" deployment mode where the API runs on a laptop and AWS holds the message bus + GitHub webhook ingress. GitHub fires webhooks at a stable public URL; the local API cannot host one. AWS must do the receiving.

**Relationship to ADR-0007.** ADR-0007 names `POST /api/v1/webhooks/github` as the canonical Treadmill webhook endpoint. This ADR **supersedes that endpoint specifically in `dev_local` and `fully_remote` modes** — the canonical receiver in those modes is the AWS-side path (API Gateway → Lambda → SQS → poller). The HTTP route survives in `fully_local` mode for fast iteration; in other modes the route returns 503 with a body that names this ADR as the explanation. ADR-0007's verbs, normalizer, and cache-then-heal Redis buffer are otherwise unchanged.

Three candidate shapes for the receive path:

- **(a) API Gateway HTTP API → SQS via `AWS_PROXY` integration** (no Lambda). The simplest topology — GitHub POSTs to API Gateway, API Gateway writes the message body directly to SQS via an integration template.
- **(b) API Gateway HTTP API → Lambda → SQS.** Lambda receives the API Gateway event, reads request body + headers, wraps both into a JSON envelope, calls `sqs.send_message`.
- **(c) Tunnel** (Cloudflare Tunnel / ngrok). Bridges localhost API to a public URL. Rejected in ADR-0016 (laptop-online dependency, provider risk).

The decision turns on **header preservation.** GitHub signs webhook bodies via HMAC-SHA256 with the result delivered in the `X-Hub-Signature-256` header. The local API verifies the signature on dequeue (the existing `treadmill_api/webhooks/signatures.py:verify_github_signature` already implements this). For verification to work, the header must survive the AWS-side hop into the SQS message body.

Investigation:

- **Option (a) does not preserve arbitrary request headers in the SQS body.** The API Gateway HTTP API → SQS integration via `AWS_PROXY` supports `MessageBody` from `$request.body` but has no Velocity-template equivalent to attach arbitrary headers into the body. REST APIs (v1) support this via templates but cost ~3.5× per request and add latency; HTTP API (v2) is what new bunkhouse-style deployments use.
- **Bunkhouse actually shipped option (a)** on 2026-01-29 (commit `d357e47e`, "Add AWS SQS infrastructure via CDK"). The original bunkhouse stack provisioned an API Gateway HTTP API with three routes (`/webhook`, `/webhook/github`, `/webhook/slack`) via `apigateway.CfnIntegration` with `integrationSubtype: 'SQS-SendMessage'` and `requestParameters: { QueueUrl: ..., MessageBody: '$request.body' }`. Body-only delivery; no header preservation in the SQS body. Per `learning:2026-05-12-precedent-history-not-just-current-state`, this pattern was retired when bunkhouse moved its API service *into* AWS (current source at `bunkhouse-stack.ts` 2026-03-18, ALB → ECS Fargate) — not because the buffered-webhook pattern was wrong, but because bunkhouse's new topology no longer needed it. **Treadmill's dev-local topology — API outside AWS — matches bunkhouse's Jan 29 topology, not bunkhouse's current topology. The right precedent to crib is the Jan 29 shape.**
- **Bunkhouse's Jan 29 pattern was body-only**; signature verification at dequeue would have required either reading the signature from the body (not possible — GitHub puts it in a header), or skipping signature verification entirely. Treadmill cannot skip signature verification (ADR-0007 requires it). So Treadmill's dev-local needs *one addition* on top of the bunkhouse pattern: a way to preserve `X-Hub-Signature-256` into the SQS body.
- **Option (b) is the AWS-blessed pattern for "do something custom between API Gateway and SQS."** A ~10-line Lambda reads `event['headers']` and `event['body']`, wraps them into a JSON envelope, writes to SQS. Adds ~50ms latency, costs ~$0 at this volume. The Lambda is the minimal addition on top of the bunkhouse Jan 29 pattern that closes the header-preservation gap.

This ADR cribs the Jan 29 bunkhouse pattern and adds the Lambda wrapper to preserve headers. **The divergence from current bunkhouse is topology-driven, not pattern-driven** — when a future Treadmill ADR moves the API into AWS, the Lambda can be deprecated in favor of the synchronous-in-API path bunkhouse now uses.

## Decision

### The receive path

`GitHub → API Gateway HTTP API → Lambda → SQS → local API webhook-inbox poller`.

Five hops; only the first three live in AWS. The local poller is a new sibling to the existing `coordination/consumer.py` and `coordination/replay.py`.

### CDK resources (part of `TreadmillCloudLite`)

The `WebhookReceiverConstruct` provisions:

- **One HTTP API** (`apigatewayv2.HttpApi`) — endpoint name `treadmill-<deployment_id>-webhook-api`. Single route at v0: `POST /webhook/github` → Lambda integration. The construct is factored to support adding routes (`POST /webhook/slack`, `POST /webhook/<other>`) without restructuring: a route table maps `route → (source_tag, lambda)`. v0 ships one entry; future multi-source ingestion is additive. The route's `source_tag` lands in the SQS envelope so the poller can dispatch to the right normalizer.
- **One Lambda function** (`lambda_.Function`) — runtime Python 3.12, ~15 lines, packaged from `infra/lambdas/webhook_receiver/`. CDK sets `environment={"WEBHOOK_INBOX_QUEUE_URL": queue.queueUrl}` on the function. IAM grants: `sqs:SendMessage` on the webhook inbox queue (via `queue.grantSendMessages(fn)`) + the `AWSLambdaBasicExecutionRole` managed policy for CloudWatch Logs (CDK applies this by default for any `lambda_.Function` and we don't override; the ADR notes it explicitly so reviewers don't think logging is absent). No VPC, no Secrets Manager, no other AWS calls.
- **One SQS standard queue** (`sqs.Queue`) — `treadmill-<deployment_id>-webhook-inbox`. Standard (not FIFO) because GitHub webhooks have no ordering guarantees. Visibility timeout **60s** — the per-message processing path is Secrets Manager fetch (cached after first call, so ~0ms steady-state) + HMAC verify (microseconds) + DB INSERT (~10-50ms) + SNS publish (~50-200ms over public internet). Steady-state ~250ms, well under 60s. Bump policy: if observed redelivery exceeds 0.1% of messages over a 24h window, raise to 120s. Retention 14 days. Subscribed to a DLQ with `maxReceiveCount=5`.
- **One SQS DLQ** — `treadmill-<deployment_id>-webhook-inbox-dlq`. Retention 14 days.
- **CloudFormation outputs** — `WebhookApiUrl`, `WebhookInboxQueueUrl`, `WebhookInboxDlqUrl`. All three written into `~/.treadmill/<deployment_id>.yaml` by `treadmill-local init` per ADR-0016. The DLQ URL is in the YAML so the operator runbook for "the DLQ has messages — what now?" can `aws sqs receive-message --queue-url <yaml.aws.webhook_inbox_dlq_url>` without first running `aws cloudformation describe-stacks`.

### The Lambda

Single file at `infra/lambdas/webhook_receiver/handler.py`:

```python
import base64
import json
import os
import boto3

sqs = boto3.client("sqs")
QUEUE_URL = os.environ["WEBHOOK_INBOX_QUEUE_URL"]
PRESERVE_HEADERS = {
    "x-github-event",
    "x-github-delivery",     # load-bearing — derives the audit-row event_id
    "x-hub-signature-256",   # load-bearing — HMAC verification
}


def handler(event, _context):
    """Wrap the API Gateway HTTP API event into a JSON envelope and enqueue."""
    headers = {
        k.lower(): v
        for k, v in (event.get("headers") or {}).items()
        if k.lower() in PRESERVE_HEADERS
    }
    body = event.get("body", "") or ""
    # API Gateway base64-encodes the body when the request is binary.
    # GitHub's webhooks are always UTF-8 JSON, but a misconfigured poster
    # could send Content-Type: application/octet-stream. Decode here so
    # the poller always sees the raw bytes the operator signed.
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8", errors="replace")
        except Exception:
            # If decode fails, pass the base64 string through; HMAC will
            # fail on the poller side, the message goes to DLQ, and an
            # operator inspects.
            pass
    envelope = {
        "headers": headers,
        "body": body,
    }
    sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps(envelope))
    return {"statusCode": 202, "body": "queued"}
```

**The Lambda does NOT verify signatures.** Verification is on the local API at dequeue. The Lambda is a pure transport adapter; anyone who finds the API Gateway URL can POST garbage to it, and the local API's dequeue-time HMAC check rejects everything without a valid signature. This separation keeps the AWS-side cost surface tiny (Lambda has no secrets fetch, no HMAC compute) and keeps signature verification in the existing well-tested code (`webhooks/signatures.py`).

### The Pydantic boundary type — `WebhookInboxEnvelope`

Per ADR-0011's "Pydantic-at-every-boundary" rule, the envelope the Lambda writes + the poller reads is a typed Pydantic model, not a raw dict. New module: `services/api/treadmill_api/webhooks/inbox_envelope.py`:

```python
from pydantic import BaseModel, ConfigDict

class WebhookInboxEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    headers: dict[str, str]  # lowercase header names; values as strings
    body: str                # the raw HTTP body (UTF-8 decoded if base64-arrived)
```

The poller calls `WebhookInboxEnvelope.model_validate_json(message_body)` on each SQS receive. Validation failure → log at WARNING, delete (poison-safe). The Lambda's `envelope = {"headers": headers, "body": body}` is shape-compatible by construction.

### Deterministic `event_id` derivation from `X-GitHub-Delivery`

GitHub assigns every webhook delivery a unique UUID in the `X-GitHub-Delivery` header. The poller derives the Event row's `event_id` deterministically:

```python
event_id = uuid.uuid5(uuid.NAMESPACE_OID, envelope.headers["x-github-delivery"])
```

Combined with the existing `ON CONFLICT (id) DO NOTHING` idempotency on the `events` table, SQS visibility-timeout redelivery cannot produce duplicate Event rows. **This promotes `X-GitHub-Delivery` from "operationally useful for tracing" (the prior draft's framing) to "load-bearing for audit-row idempotency."**

The HTTP route at `routers/webhooks.py` should be amended to use the same derivation when it's still active (fully_local mode) so the two ingress paths agree on `event_id` semantics.

### The local poller — `coordination/webhook_inbox.py`

New module sibling to `coordination/consumer.py`. Mirrors the existing consumer's structure **and inherits its Phase-3-closure fixes**:

- Long-poll the webhook inbox queue (SQS `receive_message` with `WaitTimeSeconds=20`).
- For each message: validate via `WebhookInboxEnvelope.model_validate_json(body)`.
- Look up the webhook secret from Secrets Manager (via boto3; cached for the poller's lifetime — re-read on every webhook is wasteful at ~$0.05/10K but caching means rotation requires poller restart; the ADR-0016 trade-off is "operator-visible rotation = better than imperceptible-rotation").
- Call `verify_github_signature(secret, body_bytes, envelope.headers["x-hub-signature-256"])`. On failure: log at WARNING with **only** the SQS message ID, `event_id` (derived from delivery UUID), and a generic "signature failed" reason. **Never log the body or repo/PR fields** on signature failure — a misrouted employer webhook would leak metadata into the wrong deployment's CloudWatch otherwise. Delete the SQS message (poison-safe — re-delivery would just fail again).
- On success: derive `event_id` from `x-github-delivery`; invoke the existing `webhooks/normalize.py` to map `(github_event, action)` → Treadmill event verb; persist the Event row with the derived `event_id` via `ON CONFLICT (id) DO NOTHING`; publish to the events SNS topic via the existing `eventbus.py:SNSEventPublisher`.
- Delete the SQS message on successful processing.

The poller inherits these Phase-3-closure-fixed behaviors from the existing `coordination/consumer.py`: exponential backoff `1, 2, 4, 8, 16, 30` seconds on SQS poll failure; `_failures_before_error_log` escalation; `_health_status` reported through a `CoordinationProbe` analogue; malformed-SQS-message poison-safe deletion. The plan's Phase C.1 enumerates each behavior explicitly so the implementation doesn't drift.

The existing `routers/webhooks.py:POST /api/v1/webhooks/github` route survives — it stays gated on `settings.deployment_mode == FULLY_LOCAL` so local-only iteration still works via direct HTTP POST. In `dev_local` and `fully_remote` modes, that route returns **503** (chosen over 404 — 503 signals "this endpoint exists but is intentionally disabled in this mode," 404 would mislead an operator into thinking the path was removed) with a body explaining the AWS-side path is canonical and citing ADR-0017.

The webhook-inbox poller wires into the API's existing lifespan handler alongside the coordination consumer + replay loop. Skipped in `fully_local` mode (no `webhook_inbox_queue_url` setting); started in `dev_local` and `fully_remote`.

### Webhook secret in Secrets Manager (improvement over bunkhouse)

The webhook secret (the per-deployment HMAC key) lives in AWS Secrets Manager at `treadmill-<deployment_id>/github-webhook-secret`. The local poller fetches it at startup via boto3.

This deviates from bunkhouse, which passes the webhook secret as a plain env var (per `bunkhouse/infrastructure/lib/constructs/compute.ts:494-498` — the `apiContainerSecrets` map omits it). The improvement: secret rotation is a single `aws secretsmanager put-secret-value` invocation; no redeploy needed. The cost: one Secrets Manager fetch at poller startup; cached in memory; on rotation the operator restarts the API to re-fetch (acceptable for a single-user dev tool).

### Header preservation contract

The poller relies on three headers being in the envelope body:

- `x-hub-signature-256` — load-bearing for HMAC verification.
- `x-github-event` — load-bearing for the `(event, action)` → verb mapping in `normalize.py`.
- `x-github-delivery` — **load-bearing for audit-row idempotency.** The poller derives `event_id = uuid.uuid5(NAMESPACE_OID, x-github-delivery)` so SQS visibility-timeout redeliveries collapse onto the same Event row via `ON CONFLICT (id) DO NOTHING`. Missing or empty → reject the message (DLQ).

The Lambda preserves exactly this set (`PRESERVE_HEADERS`). Adding a header to the preserve list is a one-line Lambda change + ADR amendment.

### What's deferred

- **Webhook ingestion for non-GitHub sources** (Slack, GitLab, etc.). Out of scope; the path generalizes — same Lambda, same queue, different routes on the same API Gateway.
- **GitHub App webhook signing** (vs. webhook secret). v0 uses PAT + webhook secret per ADR-0016; App migration is a future ADR.
- **In-AWS Treadmill API** (`fully_remote` per ADR-0016). When that lands, the **default disposition is that `TreadmillCloudFull` still includes `WebhookReceiverConstruct`** — the Lambda + SQS path is cheap, well-tested, and keeps the ingress path uniform across deployment modes. Collapsing into a synchronous-in-API HMAC handler (bunkhouse's current shape) is an *option* if the operator wants to shed the Lambda; that decision belongs to the future `TreadmillCloudFull` ADR, not this one.

## Bunkhouse precedent

- **Bunkhouse shipped option (a) on 2026-01-29** in commit `d357e47e` — `BunkhouseStack` in `infrastructure/lib/bunkhouse-stack.ts` (original version) provisioned an HTTP API with three webhook routes, `CfnIntegration` with `integrationSubtype: 'SQS-SendMessage'`, IAM role for API Gateway, body-only delivery via `requestParameters: { MessageBody: '$request.body' }`. The pattern was real, deployed, and operational.
- **Bunkhouse retired the pattern** when it moved its API service into AWS (subsequent commits added `LoadBalancerConstruct`, `ComputeConstruct`, `DatabaseConstruct`, etc., with synchronous HMAC verification in the FastAPI handler — current state at `bunkhouse-stack.ts` 2026-03-18). The retirement was driven by **bunkhouse's topology change** (API moved into AWS), not by the buffered-webhook pattern being wrong. Per `learning:2026-05-12-precedent-history-not-just-current-state`, "stale" doesn't mean "wrong" — different topology may resurrect the older pattern.
- **Treadmill's dev-local topology matches bunkhouse's Jan 29 topology.** The API is outside AWS in both cases. This ADR cribs bunkhouse's Jan 29 shape (API Gateway HTTP API → SQS) and **adds** the Lambda wrapper (~10 lines) to close the header-preservation gap. The Lambda is the only piece bunkhouse Jan 29 didn't have, because bunkhouse's Jan 29 consumer didn't appear to perform signature verification — that's a Treadmill addition driven by ADR-0007's signature-verification requirement.
- **Forward-compat with bunkhouse's current shape**: if a future Treadmill ADR moves the API into AWS (the eventual `TreadmillCloudFull` per ADR-0016), the Lambda can be deprecated in favor of the synchronous-in-API HMAC pattern bunkhouse now uses. Both shapes are valid; topology determines which is right.

## Trade-offs

- **One more AWS resource type (Lambda) to provision + manage.** Mitigation: the Lambda is 10 lines of Python, has no business logic, has one IAM grant. The maintenance surface is minimal.
- **~50ms latency added per webhook** (Lambda cold start is ~200ms first call; warm calls are ~5ms; SQS write adds ~10ms). At Treadmill's webhook volume (single-digit per minute), latency is invisible.
- **Signature verification lives in two places** — the existing HTTP route (`routers/webhooks.py`, used only in fully-local mode for fast iteration) AND the new poller (`coordination/webhook_inbox.py`). Mitigation: both call the same `verify_github_signature` helper from `webhooks/signatures.py`. No duplication of crypto logic; only of invocation surfaces.
- **Treadmill's pattern matches bunkhouse's Jan 29 shape, not current bunkhouse.** Topology-driven, not pattern-driven. If Treadmill ever moves to a fully-AWS deployment (`TreadmillCloudFull`), this Lambda can be deprecated in favor of the synchronous-in-API HMAC pattern bunkhouse now uses; the divergence dissolves automatically.
- **The Lambda has its own IAM role + permissions surface.** Mitigation: one permission (`sqs:SendMessage` on one queue). Auditable; the ADR documents the exact scope.

## Alternatives considered

- **Option (a) — API Gateway HTTP API → SQS direct integration.** Rejected: doesn't preserve `X-Hub-Signature-256`. The header is load-bearing for signature verification per ADR-0007.
- **Option (a) variant — API Gateway REST API (v1) with Velocity templates.** Rejected: ~3.5× cost per request, higher latency, and the value (avoiding the Lambda) is marginal at this scale. The Lambda costs nothing at v0 volumes; the REST API costs real money.
- **Option (c) — Cloudflare Tunnel / ngrok bridging localhost.** Rejected in ADR-0016: laptop-online dependency for webhook delivery.
- **No webhook signature verification at all.** Rejected: ADR-0007 requires it; GitHub recommends it; anyone who finds the API Gateway URL could otherwise inject fake events.
- **Signature verification in the Lambda.** Rejected: the Lambda would need to fetch the webhook secret from Secrets Manager on every invocation, adding latency + cost; and Treadmill already has a well-tested signature verifier in the API. Keep the verification where the code is.
- **Synchronous HMAC + processing in the Lambda; SQS only for deferred work.** Rejected: this is reinventing the API. The Lambda would need DB access, SNS access, the full normalizer logic. At that point we'd have moved the API into AWS, which violates ADR-0016's "compute is local" decision.
- **Reuse the existing coordination queue for webhook inbox.** Rejected: the coordination queue carries step lifecycle events from the worker; mixing GitHub webhook envelopes into the same queue would force the consumer to discriminate on shape. Separate queue is clearer + the consumer code path is already factored to support multiple pollers.
- **Webhook secret in env var (bunkhouse-style).** Rejected: rotation requires container restart at minimum; Secrets Manager rotation is atomic. Treadmill improves on bunkhouse here.

## Open questions

- **Q17.a — Should the local poller cache the webhook secret or re-read on every webhook?** Re-read costs ~$0.05/10K Secrets Manager fetches (effectively $0). Cached means rotation requires consumer restart. Recommend cached at v0 (simpler code path); add a `secret_refresh_interval` setting if rotation cadence becomes a real concern.
- **Q17.b — Does the API Gateway HTTP API need a custom domain?** v0 uses the auto-generated `*.execute-api.<region>.amazonaws.com` URL. GitHub doesn't care about pretty URLs. Custom domain costs ~$0 (Route 53 + ACM cert) but adds setup. Defer.
- **Q17.c — Should the Lambda run inside a VPC?** No — it talks only to SQS via the public API, no in-VPC resources, no NAT cost. Public Lambda is cheaper + lower-latency.
- **Q17.d — Should the webhook inbox queue support batch consumption?** SQS `receive_message` supports up to 10 messages per call. The current consumer pattern (per `coordination/consumer.py`) reads one at a time. Multi-message batching is a future performance optimization; v0 stays single-message for simplicity.

## Consequences

- The `WebhookReceiverConstruct` is a new module under `infra/treadmill_infra/constructs/`. `TreadmillCloudLite` composes it; the default disposition for future `TreadmillCloudFull` is to include it as well (see "What's deferred" — collapse into in-API HMAC is an option, not the default).
- The Lambda packaging path uses CDK's `aws_lambda.Code.from_asset("infra/lambdas/webhook_receiver")` — the source lives in the infra package alongside the CDK, packaged automatically at `cdk synth` time.
- A new test surface: `infra/tests/test_webhook_receiver_lambda.py` exercises the Lambda's wrap-and-enqueue logic with a mocked boto3 SQS client. Unit tests are fast; no live AWS required.
- The local poller is new code at `services/api/treadmill_api/coordination/webhook_inbox.py` with its own integration test (live SQS + real-shape envelope). Gated on `TREADMILL_INTEGRATION=1` like other integration tests.
- The existing `routers/webhooks.py:POST /api/v1/webhooks/github` route gains a guard: when `settings.deployment_mode == DEV_LOCAL` (or future `FULLY_REMOTE`), it returns 503 — the AWS-side path is canonical, the HTTP route is fully-local-only.
- The Week-4 transition plan sequences this work: CDK construct + Lambda first, then poller, then end-to-end smoke against a real GitHub repo.
