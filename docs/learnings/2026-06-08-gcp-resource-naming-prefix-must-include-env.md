---
date: 2026-06-08
trigger: surprise
status: captured
---

# Learning: GCP resources have `medicoder-` prefix + `{env}-` topic prefix; scripts must not assume bare names

## Trigger
During Plan C smoke chain testing, `smoke-end-to-end.sh` defaulted to
`webhook-attachments-${ENV}` for the GCS bucket and `topics/attachment` for
the Pub/Sub topic. Both were wrong:
- Actual bucket: `medicoder-webhook-attachments-dev`
- Actual topic: `dev-attachment` (full path: `projects/care-transitions-testing/topics/dev-attachment`)

The upload succeeded (separate gcloud auth) but the publish hit `Resource not found
(resource=attachment)` 404. The mismatch was invisible until the smoke script ran
end-to-end.

## Observation
GCP resources in `care-transitions-testing` follow two naming conventions:
1. **Pub/Sub topics**: `{env}-{name}` (e.g., `dev-attachment`, `dev-tag`, `dev-notification`)
2. **GCS buckets**: `medicoder-{purpose}-{env}` (e.g., `medicoder-webhook-attachments-dev`,
   `medicoder-webhook-attachment-text-dev`)

Scripts that hardcode bare resource names (`webhook-attachments-${ENV}`,
`topics/attachment`) will silently fail at publish/read time even if GCS upload
succeeds.

## Generalization
Any new script that touches GCP storage or Pub/Sub must derive its defaults from
the actual deployed resource names (via `gcloud pubsub topics list` or
`gcloud storage buckets list`) rather than guessing a convention. The `medicoder-`
prefix on GCS and the `{env}-` prefix on Pub/Sub topics are both load-bearing.

The OCR service env var `ATTACHMENT_BUCKET=medicoder-webhook-attachments-dev`
is the authoritative source for the correct bucket name.

## Proposed rule
When adding GCP resource defaults to a script, derive the name from an existing
authoritative source (service env var via `gcloud run services describe`, or list
the actual resources) rather than guessing from a naming pattern.

## Proposed remediation
Add a smoke-script `--dry-run` flag that validates all configured resource names
exist before attempting uploads/publishes. Alternatively: a pre-flight check at
the top of smoke-end-to-end.sh that does `gcloud storage ls gs://${ATTACHMENT_BUCKET}` and
`gcloud pubsub topics describe ${ATTACHMENT_TOPIC}` before proceeding.
