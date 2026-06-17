---
date: 2026-06-08
trigger: surprise
status: captured
related: ramjac promote-to-dev, d5e583e2 deploy
---

# Learning: Artifact Registry image not queryable for ~70s after push

## Trigger
In the ramjac Plan C sprint, `promote-to-dev` ran 26 seconds after `build-and-push` completed.
The image was in AR but `gcloud artifacts docker images describe` returned "Image not found"
for `rest_api:d5e583e` — even though querying the same tag 10 minutes later returned the digest
immediately. The skip path left `Pulumi.dev.yaml`'s old image URI in place, and `pulumi up`
deployed the stale image.

## Observation
There is a propagation window of roughly 60–90 seconds between `docker push` completing (as
reported by the GitHub Actions build step) and the image being queryable via
`gcloud artifacts docker images describe`. During this window, the describe command returns a
non-zero exit code even though the image is present in AR.

## Generalization
Any workflow that reads AR metadata immediately after a push can observe the "not found" window
regardless of actual availability. The safe pattern is either: (a) query with retries / a brief
sleep, or (b) accept the skip and ensure the downstream deploy handles the stale state correctly.

## Proposed rule
In promote-to-dev image-URI update loops, retry the `gcloud artifacts docker images describe`
command up to 3 times with a 30s sleep before concluding the image is absent.
Alternatively, when the image is absent and the key was previously set, keep the existing
config value rather than silently deploying the last committed value.

## Proposed remediation
Add retry logic to the `Update Pulumi image URIs from current build` step in `promote-to-dev.yml`:
```bash
for attempt in 1 2 3; do
  if DIGEST=$(gcloud artifacts docker images describe "${uri}" --format='value(image_summary.digest)' 2>/dev/null); then
    break
  fi
  [ "$attempt" -lt 3 ] && echo "Waiting for image propagation (attempt $attempt)..." && sleep 30
done
```

## Notes
The regression happened because `Pulumi.dev.yaml` has `restApiImageUri` locked to the initial
image SHA. When the skip path is taken, `pulumi up` reads the file and deploys the initial image
— but only if a different config key changed in the same commit (here: `otelCollectorEndpoint`)
triggers a Cloud Run update. Without any config diff, `pulumi up` would be a no-op. The interaction
of "skip on propagation race" + "unrelated config change triggers update" caused the regression.
