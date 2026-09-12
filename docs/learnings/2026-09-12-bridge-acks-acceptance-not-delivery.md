---
date: 2026-09-12
trigger: surprise
status: captured
related: ADR-0102, ADR-0104, ADR-0107
---

# Learning: The sibling bridge acks queue acceptance, not turn delivery

## Trigger
Gerald's ADR-0107 cross-model review looked slow. We first attributed the delay to
the reviewer ("slow, not incapable"). Gerald then ground-truthed his own bridge and
falsified that attribution.

## Observation
Gerald checked `~/gerald/bridge/ledger/`: 21 of 21 records held `"state":"submitted"`
with an empty `turnIds` array. Not one record advanced. Cause: `deliver-inbound.mjs`
shells out to `inbound.mjs deliver`, a one-shot CLI. The `deliver` path sets
`record.queueAcknowledgedAt` (inbound.mjs:183) and returns. It never starts the
watcher. The turn-delivery machinery exists — `reconcile()` (inbound.mjs:215) parses
the rollout JSONL for `task_started`/`task_complete` and advances
submitted→running→completed — but it only runs from `watch`/`serve` or the `scan`
subcommand. None of those runs. `INBOUND.md:53` documents the trap in plain words.

## Generalization
An ack that fires at admission, not at completion, makes every record look terminally
delivered forever. There is no signal that distinguishes "queued and unread" from
"delivered and being worked". The one message class where a stale read is harmful — an
operator STOP or HOLD — is the class with no read receipt. We tend to trust an ack
without asking which lifecycle point it marks.

## Proposed rule
An ack must mark the lifecycle point it claims. A "delivered" ack must fire on turn
delivery, not on queue acceptance. If a bridge cannot confirm delivery, it must not
report a state that reads as delivered.

## Proposed remediation
Cheapest first: (a) run `inbound.mjs watch` (or a periodic `scan`) under
`gerald-bridge-supervise.sh` and the Fran equivalent, so `reconcile()` advances records
and emits `committed`; (b) or switch `deliver-inbound` to the `serve` interface.
Follow-up: expose a delivery-confirmation query so an operator STOP has a read receipt.
Same defect exists in the Fran bridge substrate (ADR-0102) that Gerald reuses.

## Notes
Evidence supplied by Gerald (open-weight sibling) from his own ledger, 2026-09-12.
`confirm-absent` already exists for recovery, so option (a) is likely sufficient.
