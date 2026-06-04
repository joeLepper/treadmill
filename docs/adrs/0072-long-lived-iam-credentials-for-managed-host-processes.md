# ADR-0072 — Long-lived IAM credentials for managed host processes

- **Status:** proposed
- **Date:** 2026-06-04
- **Related:** ADR-0018 (autoscaler in dev-local), ADR-0019 (host-side credential injection),
  ADR-0069 (managed host processes self-heal)

## Context

The three managed host processes — autoscaler, deploy-watcher, and scheduler — each receive
AWS credentials when they are spawned by `treadmill-local up`. Today those credentials come
from the operator's SSO session via `AWS_PROFILE` in the subprocess env.

This works for individual developers but fails for two cases:

1. **Unattended / CI-like hosts** (long-lived EC2 instances, shared dev boxes): the operator
   has no interactive SSO session. `AWS_PROFILE` pointing at a stale OIDC cache causes every
   boto3 call to fail with `ExpiredTokenError`.

2. **Credential rotation while the process is alive**: even when SSO works, an operator session
   can expire mid-day. The host process inherited the env at spawn time and has no refresh path
   — it will start returning 403 from any SQS/SNS call until the next restart.

The fix requires a seam where a separate credential-vending mechanism can inject long-lived IAM
credentials (access-key + secret + optional session token) into the subprocess env, displacing
the SSO profile. The file at `~/.treadmill/managed-host-credentials.json` is that seam — it is
machine-local, mode-0600, never committed (`.gitignore`-protected), and populated by whatever
rotation/bootstrap tooling the operator chooses (Vault, AWS IAM credential rotation cron,
`aws iam create-access-key`, etc.).

Failure must be **loud**: if the file exists but is unreadable or malformed, the process must
refuse to start rather than silently falling back to SSO. A half-broken credentials file is an
operator error, not a "try the old path" signal.

## Decision

When `_start_autoscaler_dev_local` or `_start_deploy_watcher_dev_local` in `runtime.py`
builds the subprocess env:

1. Call `resolve_managed_host_credentials()` from the new
   `treadmill_local.managed_credentials` module.
2. **File absent** → return `None`; caller does nothing; `AWS_PROFILE` stays in env (existing
   SSO path unchanged).
3. **File present + valid** → return `{"AWS_ACCESS_KEY_ID": ..., "AWS_SECRET_ACCESS_KEY": ...,
   ["AWS_SESSION_TOKEN": ...]}`. Caller drops `AWS_PROFILE` from env and merges the dict in.
   boto3 in the subprocess sees only explicit env-var credentials — no profile lookup.
4. **File present + broken** → raise `ManagedCredentialsFileError`. Caller logs and re-raises;
   the spawn fails loudly. Never falls back to SSO.

The resolver is a standalone function in `managed_credentials.py` — no coupling to runtime
internals — so it is independently testable with `tmp_path` fixtures and trivially reusable
when the scheduler spawn site gets the same treatment (follow-up; out of scope here because
the scheduler runner lives in `services/api/` and spans a different package boundary).

File schema (`~/.treadmill/managed-host-credentials.json`):

```json
{
  "access_key_id": "AKIA...",
  "secret_access_key": "...",
  "session_token": "...",   // optional; include for federation / assumed-role creds
  "expires_at": "2026-12-31T23:59:59Z"  // optional; informational only
}
```

## Sequence

```mermaid
sequenceDiagram
    participant Op as treadmill-local up
    participant R as runtime._start_autoscaler_dev_local
    participant MC as managed_credentials.resolve()
    participant FS as ~/.treadmill/managed-host-credentials.json
    participant Sub as autoscaler subprocess

    Op->>R: spawn autoscaler
    R->>R: build env dict (AWS_PROFILE set)
    R->>MC: resolve_managed_host_credentials()
    MC->>FS: path.exists()?
    alt file absent
        FS-->>MC: False
        MC-->>R: None
        R->>Sub: Popen(env with AWS_PROFILE)
    else file present + valid
        FS-->>MC: True
        MC->>FS: read_text() + json.loads()
        MC-->>R: {AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, ...}
        R->>R: env.pop("AWS_PROFILE"); env.update(managed)
        R->>Sub: Popen(env with IAM key vars, no AWS_PROFILE)
    else file present + broken
        FS-->>MC: True
        MC->>FS: read_text() → OSError or json.loads() → JSONDecodeError
        MC-->>R: raise ManagedCredentialsFileError
        R->>R: logger.exception(...); raise
        Note over R,Sub: spawn aborted — loud failure
    end
```

## Alternatives considered

- **Re-read the file every tick** (credential refresh without restart). Adds coupling between
  the control loop and credential state; complicates the simple env-var model without closing
  the restart-on-rotation gap (boto3 sessions cache internally anyway). Out of scope; the
  operator can rotate by restarting the host process — ADR-0069 makes that cheap.
- **Always require a credentials file; remove AWS_PROFILE fallback.** Cleaner contract but
  breaks the common single-developer laptop flow. SSO is fine for interactive use; the file
  path is an opt-in for unattended hosts.
- **Store creds in AWS Secrets Manager.** Circular — the host process needs AWS creds to read
  from Secrets Manager; bootstrapping problem is the same.
- **Instance role / IMDS.** Works on EC2 but not on laptops. Out of scope here; the resolver
  returns `None` when the file is absent and boto3's credential chain handles IMDS naturally.

## Consequences

### Good

- Unattended hosts and shared dev boxes get a stable AWS credential path that does not depend
  on interactive SSO sessions.
- Loud failure on malformed file prevents silent permission errors hours later.
- Resolver is a pure function over the filesystem — fully unit-testable, zero coupling to
  runtime internals.
- SSO path is completely unchanged for operators who don't create the file.

### Bad / trade-offs

- Two credential modes make the local-adapter slightly harder to reason about in the abstract;
  mitigated by the clear "file absent = SSO, file present = IAM keys" rule and the module-level
  docstring.
- The scheduler is out of scope for this ADR (it lives in `services/api/`); unattended hosts
  must still have SSO for the scheduler until a follow-up applies the same pattern there.
- Long-lived access keys carry rotation obligations that SSO does not. Operators who use this
  path own the rotation — the ADR does not prescribe a rotation mechanism.
