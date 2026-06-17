---
date: 2026-06-08
trigger: surprise
status: captured
related: promote-to-dev runs 27126103085
---

# Learning: Scraper-v2 images live in a separate AR project with a different registry, repo, and tag scheme

## Trigger
promote-to-dev run 27126103085 failed immediately with "Image not found" for `scraper-v2-scheduler` and `scraper-v2-service`. The Pulumi code pointed to `us-west2-docker.pkg.dev/${projectId}/ramjac/scraper-v2-scheduler:latest` — a registry/project/repo/tag combination that had never existed.

## Observation
Scraper-v2 services are built by `build-and-push-scrapers.yml`, a separate workflow intentionally decoupled from the main `build-and-push.yml`. The actual image path is `us-west1-docker.pkg.dev/ramjac-artifacts/services/scraper_v2_scheduler:${git_sha}`. Four differences from what Pulumi expected: region (us-west1 not us-west2), project (`ramjac-artifacts` not the project variable), repo (`services` not `ramjac`), tag (SHA not `latest`), and service name uses underscores not hyphens. The build workflow additionally requires a `roles/artifactregistry.writer` IAM grant on the `ramjac-artifacts` project that had not been made — so the images may never have been pushed.

## Generalization
Services owned by different teams or using different AR projects need to be cross-referenced against their actual build workflow (not assumed to follow the main pattern) before Pulumi image URIs are written. The `build-and-push-scrapers.yml` comment explicitly documents the prerequisite IAM grant and the intentional decoupling — this was available and was not read.

## Proposed rule
Before authoring a Pulumi Cloud Run image URI: read the service's actual build workflow to confirm the registry, project, repo name, naming convention (hyphens vs underscores), and tag strategy. Do not assume the main `build-and-push.yml` pattern applies.

## Proposed remediation
none yet — could add a CI lint that cross-references image URIs in Pulumi with known AR paths, but the maintenance cost is high.

## Notes
The fix was to comment out the two scraper-v2 service registrations from index.ts with a clear comment pointing to the prerequisite IAM grant. Re-enabling them requires: (a) the grant is made, (b) the build runs successfully, and (c) the image URI in Pulumi is corrected to the actual path.
