# IAM token drift — SSO/IAM credentials expire mid-flight

**Related:** ADR-0072 (credential refresh), ADR-0075 (operator obligations)

## Symptom

A workflow step that was progressing normally suddenly fails with authentication or permission errors. The worker has been running for hours but the credentials it cached at startup are now expired. Error messages include:
- `InvalidSignatureException` (token signature doesn't verify)
- `AuthFailure` or `UnauthorizedException` (token rejected)
- `AccessDenied` with `SignatureDoesNotMatch` (signature invalid, token expired)
- `Token is expired` or similar from the auth service

## Root cause checklist

- [ ] **Credential TTL expired**: The IAM session token or SSO token was issued with a short TTL (15 min, 1 hour) and the step took longer than expected. Check:
  - The `Expiration` timestamp on the credential (if visible in logs or env vars)
  - Whether the step's actual runtime exceeds the token's TTL

- [ ] **Ambient credentials not refreshed**: The worker process cached credentials at startup and didn't call `assume_role()` or refresh the token during the step. Verify the worker is using an SDK with auto-refresh enabled, or manually refreshing credentials before each API call.

- [ ] **Cross-account or cross-region role assumption**: The worker assumes a role in a different account and the temporary credentials it got are not being refreshed. The assume-role session may have its own TTL independent of the primary credential.

- [ ] **SSO token revoked or invalidated**: The user's SSO session was terminated (logged out, password changed) mid-step. The cached access token is now invalid. This is distinct from expiry—the token didn't age out, it was actively revoked.

- [ ] **Timezone or clock skew**: The worker's system clock is ahead or behind, causing the signature to be rejected (AWS signatures include a timestamp). Verify `date` on the worker and compare to a known-good clock.

## Commands

**Inspect worker logs for auth errors:**

```bash
# Dev-local
tail -f .treadmill-local/worker-*.log | grep -i "expired\|signature\|unauthorized\|access.denied"

# Cloud (CloudWatch)
aws logs tail "/ecs/treadmill-worker" --follow --filter-pattern "expired OR Unauthorized OR AccessDenied"
```

**Check credential expiration metadata (if available in logs):**

```bash
# If logs contain the credential, look for "Expiration" field
grep -i expiration .treadmill-local/worker-*.log
```

**Verify system clock on the worker:**

```bash
date
# Should match `date -u` on your local machine or a known-good NTP server
# If skewed by >5 min, NTP sync is broken
ntpdate -q <ntp-server>  # or check systemd-timesyncd status
```

**Check if the credential refresh hook is running:**

For workers using AWS SDK v2 or higher, the SDK should auto-refresh credentials. Verify:

```bash
# AWS SDK Java
grep -i "refresh\|credential" .treadmill-local/worker-*.log | head -20

# AWS SDK Python (boto3)
# Enable debug logging: AWS_DEBUG=true or set logging level to DEBUG
# Look for "RefreshingToken" or "Fetching credential"
```

**Inspect the IAM policy for session duration:**

```bash
aws iam get-role \
  --role-name "<worker-role>"
# Look for MaxSessionDuration in the response (default 3600 seconds = 1 hour)
```

For cross-account assume-role:

```bash
aws sts assume-role \
  --role-arn "arn:aws:iam::<account>:role/<role-name>" \
  --role-session-name "debug-session" \
  --duration-seconds 3600  # Session lifetime
```

The response's `Credentials.Expiration` is the token's TTL.

## Durable fix

**Short term (unblock in-flight steps):**

1. **For expired tokens**: Refresh credentials immediately:
   - **AWS SDK**: Call `assume_role()` or trigger the SDK's credential refresh manually.
   - **SSO**: Re-authenticate with SSO provider, or use `aws sso login` to refresh the cached token.
   - Restart the worker or trigger a new step execution with fresh credentials.

2. **For revoked tokens**: Re-authenticate and obtain new credentials.

3. **For clock skew**: Sync the worker's clock:
   ```bash
   timedatectl set-ntp on  # Linux with systemd
   ntpdate -s <ntp-server>  # or use pool.ntp.org
   ```

**Long term (prevent recurrence):**

- **Enable credential auto-refresh**: Ensure the worker uses an SDK version with built-in credential refresh (AWS SDK v2+, boto3 4.0+). Do not cache credentials in memory without a refresh strategy.

- **Implement credential refresh hooks**: Add a pre-work-step check that refreshes credentials if their TTL is <50% remaining. Example:
  ```python
  creds = get_current_credentials()
  if time_until_expiry(creds) < ttl / 2:
    refresh_credentials()
  ```

- **Document and enforce session duration**: Set `MaxSessionDuration` on worker roles to be at least 2x the longest expected step duration. If steps can take up to 2 hours, set session duration to 4+ hours.

- **For cross-account assume-role**: Include credential refresh in the assume-role loop:
  ```python
  def get_cross_account_creds():
    creds = sts.assume_role(...)
    store_with_expiry(creds)  # Store expiry timestamp
    return creds
  
  def work_step():
    creds = get_cross_account_creds()
    if approaching_expiry(creds):
      creds = get_cross_account_creds()  # Refresh mid-step
    # ...
  ```

- **Monitor and alert on auth failures**: Add a metric that increments on `InvalidSignatureException` or `AccessDenied` errors. Alert if the rate exceeds a threshold (e.g., >5 per minute across the fleet). This catches token-drift issues early, before users report stalled tasks.

- **Run NTP sync on workers**: For cloud-deployed workers, ensure the base image has NTP enabled and synchronized. For dev-local, periodic `ntpdate` checks or systemd-timesyncd should keep the clock accurate.

- **Cross-link ADR-0072**: For detailed credential refresh architecture and decision context, see ADR-0072. It documents the refresh policy, who is responsible for refresh, and how to validate that refresh is happening.
