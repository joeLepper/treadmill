---
date: 2026-06-08
trigger: surprise
status: captured
related: medicoder Plan C, diagnosis-entity-detector-dev, promote-to-dev
---

# Learning: Pulumi 409 Conflict when Cloud Run service updated outside Pulumi

## Trigger
`promote-to-dev` failed on `diagnosis-entity-detector-dev` with:
```
Error 409: Conflict for resource 'diagnosis-entity-detector-dev':
version '1780914179749950' was specified but current version is '1780914479816525'.
```
Pulumi waited 1200 seconds (20 minutes) before surfacing the error. The root cause: the DED
service was manually updated with `gcloud run services update --update-env-vars` in the previous
session to unblock a crash-loop (`OTEL_EXPORTER_OTLP_ENDPOINT`). This incremented the Cloud Run
resource version but Pulumi's stored state still held the old etag/version.

## Observation
Cloud Run uses etag-based optimistic concurrency on updates. Pulumi stores the last-known etag in
its stack state. When a resource is updated outside Pulumi, the etag advances. The next
`pulumi up` sends the stale etag → GCP returns 409. Pulumi's retry budget is exhausted after
1200 seconds before it gives up.

## Generalization
Any time a Pulumi-managed Cloud Run service is updated via `gcloud run services update` (even for
a one-time env-var fix), the stored etag drifts. The next Pulumi deploy will hang for 20 minutes
then fail unless the state is refreshed first.

## Proposed rule
Never use `gcloud run services update` on a Pulumi-managed Cloud Run service as a workaround.
Instead: either set the env var via Pulumi config and push a commit, or run `pulumi refresh`
immediately after the manual update to re-sync the etag.

If the manual update is already done and the drift is present, run
`pulumi refresh --stack <stack> --non-interactive --yes` before `pulumi up`.

## Proposed remediation
Added `pulumi refresh --non-interactive --yes || true` before `pulumi up` in
`promote-to-dev.yml` (commit `b4aa6821b`). The `|| true` ensures a refresh failure
(e.g. short-lived auth issue) does not block the deploy; the 409 would then self-heal
on the next run after the token is valid.

## Notes
The correct long-term fix for the OTEL crash-loop was to add `otelCollectorEndpoint` to
`Pulumi.dev.yaml` (committed in `d5e583e2d`) and redeploy via the pipeline. The manual
`gcloud run services update` was a faster workaround but created this drift. The rule:
use Pulumi config for permanent env-var changes; reserve `gcloud` one-liners for true
production incidents where Pulumi pipeline latency is unacceptable.
