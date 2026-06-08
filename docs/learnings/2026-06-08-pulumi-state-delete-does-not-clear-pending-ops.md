---
date: 2026-06-08
trigger: surprise
status: captured
related: promote-to-dev runs 27123825358, 27126103085
---

# Learning: `pulumi state delete` per-URN does not reliably clear pending operations

## Trigger
promote-to-dev run 27126103085 failed with "Attempting to deploy with 12 pending operations" even though the workflow step before `pulumi up` ran `pulumi state delete "$urn" --yes --force` for every pending-create URN extracted from the exported state. The deletes were silenced with `|| true`, so failures were invisible.

## Observation
`pulumi state delete` removes a resource entry from the state snapshot. Pending operations are tracked in a separate `deployment.pending_operations` array — these are not resource entries; they are in-flight operation records. Deleting the resource URN from state does not atomically clear its entry in `pending_operations`. The next `pulumi up` still sees the pending ops and retries the creates, hitting 409 (Already Exists) after 1200s.

## Generalization
The correct non-interactive repair for pending-create operations is to zero the `pending_operations` array via state import, not to delete resource URNs one by one. Pulumi itself recommends this in its troubleshooting docs: export the state, set `pending_operations = []` with jq, and re-import.

## Proposed rule
When clearing Pulumi pending operations non-interactively: use `pulumi stack export | jq '.deployment.pending_operations = []' > clean.json && pulumi stack import --file clean.json`. Never use `pulumi state delete` per-URN for this purpose.

## Proposed remediation
CI step linting: any promote-to-dev workflow change that introduces `pulumi state delete` in a pending-op-clearing loop should be flagged for review.

## Notes
The `|| true` silencing pattern is load-bearing in CI (don't abort on a non-fatal error) but makes silent failures invisible. Pair with an explicit progress echo before each delete so failures are at least visible in the log.
