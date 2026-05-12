# ADR-0019: Host-side credential injection for dev-local containers

- **Status:** proposed
- **Date:** 2026-05-12
- **Related:** ADR-0016 (supersedes Q16.c), ADR-0018

## Context

ADR-0016 Q16.c committed dev-local mode to "long-lived IAM-User access keys for the worker." The mechanism — mount the operator's `~/.aws` directory into the container so boto3's default credential chain finds the operator's SSO profile, then `startup_auth.resolve_worker_aws_session` uses that SSO to fetch the worker's IAM keys from Secrets Manager, then rebuilds the session with those keys. This worked through Phase D and the first end-to-end smoke.

The first time we ran a second worker more than an hour after `aws sso login`, the design broke. The cached SSO access token had expired. Boto3 tried to refresh it (atomic temp-file write to `/root/.aws/sso/cache/`). Two failure paths:

- **`:ro` mount.** Write fails with `OSError: [Errno 30] Read-only file system: /root/.aws/sso/cache/tmpXX.tmp`. The worker exits at startup with `StartupAuthError`. The autoscaler (ADR-0018) would spawn a fresh worker per message; every message after the first hour silently fails. The wf-review smoke-step hit exactly this.
- **`:rw` mount.** Write succeeds, but the container ran as root, so the refreshed token file is now `-rw------- 1 root root` on the host. The operator's next `aws sso login` fails with `Permission denied`. Recovering requires `sudo chown` and operator-side hand-fixing. Observed live during the smoke session.

The cache-mount path is structurally wrong, not contextually wrong: any long-running container that uses SSO via host-mount-share hits this. The mistake was inheriting the "let boto3 find credentials the way it normally does" pattern without accounting for SSO's writeback expectations. Static credentials (long-lived IAM keys, env vars, profiles without SSO) avoid the problem because boto3 doesn't write them back.

The shape of the fix surfaces itself: workers don't need *credentials in a file* — they need *credential values in their process env*. The local-adapter is already a host-side process with operator SSO; it can fetch the worker's IAM keys once at startup and pass them as env vars to the spawned container.

## Decision

### The container never authenticates with SSO

Workers, the API container, and any other dev-local container Treadmill spawns get their AWS credentials as `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` environment variables, set by the local-adapter at container-spawn time. The container's boto3 reads them via the standard env-var credential resolution. **No `~/.aws` mount.** **No bootstrap-session.** **No worker-side SSO logic at all.**

`startup_auth.resolve_worker_aws_session` collapses to `boto3.Session(region_name=settings.aws_region)` — the env vars already in scope are what boto3 picks up. The bootstrap-then-worker code path retires.

### The local-adapter fetches the worker IAM keys once per `up` and caches them

`treadmill-local up --deployment <id>` calls `aws secretsmanager get-secret-value --secret-id treadmill-<id>/worker-aws-credentials` on the host (using the operator's SSO via `AWS_PROFILE`). It parses the JSON `{aws_access_key_id, aws_secret_access_key}`, holds the values in memory for the lifetime of the up-process, and injects them as env vars on every container it spawns (API + autoscaler-spawned workers + manual `run-worker` workers).

When the autoscaler spawns a worker (ADR-0018), it inherits the env from the local-adapter parent process — including the credential env vars. No re-fetch per worker; one secret read per `up` invocation.

### The API still uses the operator's SSO profile

The API runs as a long-lived dev-time container that the operator interacts with directly. It uses the operator's SSO via `AWS_PROFILE` and the standard env-var-driven credential resolution. **It does not need `~/.aws` mounted** because (per below) we use the env-var path instead.

Concretely: the local-adapter, before spawning the API container, calls `aws sts get-caller-identity` to verify the SSO token is fresh. It then exports the SSO-session credentials (access key id + secret + session token, via `aws configure export-credentials --profile <op>` or the boto3 equivalent) and injects them as env vars on the API container. **The container never sees the SSO cache.** When the token expires (typically 1h), the API's next AWS call returns `ExpiredTokenException`; the operator runs `aws sso login` on the host and `treadmill-local up --deployment <id>` re-injects fresh creds.

This is a small UX wart — long-running API sessions need an explicit re-inject every hour. Acceptable for v0; the alternative is operator-side daemon code we don't want to write yet.

### Worker IAM-user permission set stays minimal

ADR-0016 Q16.c's least-privilege policy doesn't change. The worker IAM user keeps its three statements (consume work queue, publish events, read github-pat). What changes is only the *delivery mechanism* for those credentials into the container — not the credentials themselves.

### Credential rotation

When the operator rotates the worker IAM keys (`aws iam create-access-key && aws secretsmanager put-secret-value && aws iam delete-access-key`), the rotation only takes effect on the next `treadmill-local up`. Running workers continue with the old keys until they exit (one-shot, ~minutes). Acceptable cadence: rotation is rare; transient overlap is fine.

The bunkhouse precedent retired this same pattern (mount-credentials) in favor of IAM-role-based assumption for ECS tasks. Dev-local is laptop-resident, so role assumption isn't available; env-var injection from a host process is the closest equivalent shape.

## Supersedes ADR-0016 Q16.c

ADR-0016 Q16.c's resolution stands as "long-lived IAM-User access keys per deployment" (the *what*). The *how* — host-SSO-mount + bootstrap-session — is wrong and is replaced by this ADR. ADR-0016 should be amended to point at this ADR for the credential-delivery mechanism while keeping its higher-level decisions (IAM-User identity, deployment_id-scoped secret, etc.) intact.

## Trade-offs

- **One more hop in the credential path.** The local-adapter now has a load-bearing role in credential management. The trade-off is "one process knows the secret value (briefly, in memory) vs every container container reaches out to AWS for it." Net: one well-tested host path is easier to reason about than N container-side bootstrap paths.
- **Restart-the-stack to rotate or refresh.** API container needs a fresh `up` to pick up new credentials. UX wart for the every-hour SSO refresh. Operator can script `aws sso login && treadmill-local up --deployment <id>` as a one-liner.
- **Secret value held in memory by the local-adapter.** The local-adapter is the operator's own process; the operator already has access to the secret via SSO. Same trust boundary. Don't pretend it's tighter than it is.
- **No `~/.aws` ownership mistakes.** The whole class of "container wrote to my SSO cache" bugs goes away. Recovering from `:rw`-induced chown breakage took manual steps during the smoke; this ADR makes that impossible.
- **Lost: emergency override via container exec.** With `~/.aws` mounted, an operator could `docker exec -it treadmill-api bash` and run `aws --profile treadmill-personal sqs receive-message ...` for ad-hoc debugging. After this ADR, that path requires the operator to either: (a) `docker exec` and use `aws --no-sign-request` with the injected creds explicitly, or (b) run the AWS CLI on the host instead. Neither is hard; flagging it in case a debugging session expects the mount.

## Alternatives considered

- **Run containers as the host operator's UID + GID.** Solves the root-owned-file problem. Doesn't solve the `:ro` SSO-refresh problem (the mount mode is the same constraint regardless of UID). Also requires `chmod` on every Dockerfile-installed directory or runtime `chown` shenanigans. Rejected: solves half the problem.
- **Sidecar credential helper that runs as the host operator's UID.** Same shape as this ADR but with a second container instead of an env-var injection. Adds complexity (process supervision, IPC) for no security gain — the local-adapter already has the operator's credentials. Rejected.
- **Use IAM Roles Anywhere for the worker.** Replace IAM-User keys entirely with role-assumption via signed certificates. Real production-grade pattern; bunkhouse uses something similar for its GPU server. Too heavyweight for v0 dev-local; the worker's keys are operator-controlled and rotatable. Defer until cross-machine deployments need it.
- **Long-lived SSO token mounted into the container, refreshed by a host helper.** The host runs `aws sso login` proactively + writes the cache file with the container's UID. Works mechanically but requires a long-lived helper process synchronized with each container's lifecycle. The env-var injection is strictly simpler.
- **Embed the IAM-User keys in the YAML config.** Defeats the purpose of Secrets Manager (revocation, audit) and puts secrets in a file the operator routinely edits. Rejected.

## Open questions

- **Q19.a — Should the local-adapter cache the credential value across `up` invocations?** Currently re-fetches on every `up`. Caching to disk would mean the host filesystem holds the IAM key plaintext, which is exactly what we're avoiding. Don't cache. The Secrets Manager fetch is a single API call ~$0.05/10k — negligible.
- **Q19.b — How does `treadmill-local status --deployment <id>` report credential freshness?** When the operator's SSO token has expired the host can't fetch new creds; the API container is running with old (still-valid) keys. Status command could `aws sts get-caller-identity` and warn on `ExpiredToken`. Defer until operators ask for it.
- **Q19.c — Does the API need a different credential set than the worker?** Today both use the same `worker-aws-credentials` secret (which the API picks up via operator SSO indirectly). Could split: an API IAM user with read-only Secrets Manager + write SNS, separate from the worker. Probably overkill at v0; single set of keys is simpler. Note for future.
- **Q19.d — How does this interact with `fully_remote` mode (future)?** In `fully_remote`, the API runs in AWS (ECS task) and uses IAM roles natively; the env-var injection mechanism doesn't apply. The worker, if also in AWS, gets task-role credentials. This ADR is dev-local-scoped only. The ADR for `TreadmillCloudFull` will handle that case.

## Consequences

- **Implementation order**: this ADR's impl (task #97) ships **before** the autoscaler impl (ADR-0018 / task #92). Sequencing is in the running log of the Week-4 plan.
- `tools/local-adapter/treadmill_local/runtime.py` gains a `_fetch_worker_credentials()` step in `_up_dev_local` (and parallel logic for the API's operator-SSO export).
- `tools/local-adapter/treadmill_local/runtime.py`'s `_volumes_for()` loses the `~/.aws` mount entirely for dev-local mode.
- `workers/agent/treadmill_agent/startup_auth.py`'s `resolve_worker_aws_session` collapses from ~50 lines to a single `boto3.Session(region_name=settings.aws_region)`. Worker tests update in lockstep.
- ADR-0016's Q16.c gets a "superseded by ADR-0019" note pointing here.
- Once this lands, ADR-0018's autoscaler implementation becomes unblocked. The two together close Phase 2 success criterion 4 (end-to-end PRs without operator-loop assistance).
