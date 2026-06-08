---
date: 2026-06-08
trigger: surprise
status: captured
related: promote-to-dev run 27130160075
---

# Learning: S3 client eagerly decodes CloudFront private key in constructor, crashing routes that never sign URLs

## Trigger
After the first successful promote-to-dev, `GET /tags` returned 500 with `ERR_INVALID_ARG_TYPE: The first argument must be of type string ... Received undefined`. The service was deployed and Prisma was connected — yet every authenticated request failed before any DB query ran.

## Observation
`service/rest_api/clients/s3.js` runs `Buffer.from(this.encodedCloudfrontPrivateKey, "base64")` in its constructor. `CLOUDFRONT_PRIVATE_KEY` is not set in the dev Cloud Run service (only staging/prod would have it). The `TagRoutes` and `TagConfigRoutes` constructors both create a new `S3` instance on every request — even though their `index()` and `create()` methods never call `createSignedUrl`. The eager constructor decode crashed every request to those routes in the dev environment.

## Generalization
AWS/CDN credentials that are only needed in some environments get decoded eagerly in constructors because the code was written and tested in envs where they're always set. Routes that import a client "just in case" (even if unused per method) inherit the crash. The combination of eager init + partial environments is a latent failure that only surfaces at first deploy to a new tier.

## Proposed rule
Client constructors that decode env-var credentials must guard with a null check (`if (key)`) rather than calling `Buffer.from(key, "base64")` unconditionally. Construction must be safe in any environment; decoding can fail lazily when the method that requires the key is actually called.

## Proposed remediation
A CI check that static-analyses for `Buffer.from(process.env.X, ...)` in constructor bodies without a preceding `if (process.env.X)` guard — or at minimum a note in the `s3.js` file. For the specific crash: fix already shipped in commit `a652919c0` (guard added to `encodedCloudfrontPrivateKey` decode).

## Notes
The `createSignedUrl` method also reads `this.cloudfrontPrivateKey` — it will throw a runtime error in dev if called, which is correct (CDN signing should never be exercised in dev). The guard makes construction safe; callers are still responsible for not calling CloudFront-specific methods without credentials.
