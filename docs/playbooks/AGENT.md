# Operator Playbooks

This directory contains playbooks for diagnosing and resolving common production issues in Treadmill.

Each playbook follows a consistent structure:
- **Symptom**: How the issue manifests to the operator.
- **Root cause checklist**: Ordered steps to identify the underlying problem.
- **Commands**: Diagnostic SQL, shell commands, and API calls to inspect the system.
- **Durable fix**: Short-term (unblock the queue/system) and long-term (prevent recurrence) actions.

These playbooks align with ADR-0075 (operator obligations) and are designed to be followed during production incidents.

## Index

- [**zero-consume-rate.md**](zero-consume-rate.md) — Workers exist but aren't completing work. Check for queue URL mismatches, IAM drift, worker crash-loops, lease expiry, or backpressure.

- [**iam-token-drift.md**](iam-token-drift.md) — SSO/IAM credentials expire mid-flight. Diagnose token expiry, revocation, clock skew, or missing auto-refresh. Cross-linked to ADR-0072 (credential refresh policy).

- [**image-build-stuck.md**](image-build-stuck.md) — Image build fails repeatedly, wedging the autoscaler and queue. Identify code errors, missing dependencies, resource exhaustion, or permission issues. Includes guidance on the `--no-build` escape hatch and long-term escalation.

## How to use

When a task or workflow step appears stuck:

1. Run the **Symptom** check to confirm the issue applies.
2. Work through the **Root cause checklist** in order.
3. Run the **Commands** for each suspected cause.
4. Apply the **Durable fix** once the root cause is identified.

If multiple playbooks seem relevant, prioritize in this order:
1. **image-build-stuck** (if workers are not spawning and autoscaler logs show build errors).
2. **zero-consume-rate** (if workers exist but the queue isn't draining).
3. **iam-token-drift** (if workers are failing with auth errors mid-step).

All playbooks assume you have:
- Database query access (for SQL diagnostics).
- SSH or container access to the worker machines.
- AWS CLI credentials with CloudWatch Logs and SQS access.
- Git access to the Treadmill repository.
