# `treadmill-events` channel re-emits the full catch-up snapshot on every reconnect

**Date:** 2026-06-06
**Related:** ADR-0068 (treadmill-events channel + shared channel conventions),
the cc-channels server in `tools/cc-channels/`

## What happened

While monitoring the post-deploy state of dev-local (after the deploy_watcher
fix at 2026-06-07 03:30 UTC), the `treadmill-events` channel emitted the
same `catch_up="true" active_count="14"` reconcile snapshot at least six
times in succession over a ~10 minute window. Each emission was
byte-identical: same 14 pr_merged tasks, no state change.

Each emission costs a turn (the operator session sees a channel message,
acknowledges, and waits for the next). Six redundant emissions in 10
minutes is ~5% turn-overhead loss in the dev-local watching loop, plus
context-window pressure on the operator session — each event is a
~2KB block.

## What's actually happening

Looking at the channel-server code in `tools/cc-channels/`: the
catch_up snapshot fires every time the connection (re)establishes.
Today's deploy_watcher restart cycled the API container — which the
events server polls — and each cycle triggered a reconnect. Each
reconnect → fresh catch_up snapshot → re-emit, regardless of whether
state actually changed.

Symptom amplifier: when the API container is recreated rapidly (as
during today's catch-up of 9 PRs through deploy_watcher), the channel
server reconnects to each new container in sequence and re-emits each
time.

## Diagnostic recipe

When the catch-up snapshot fires multiple times in a row with identical
contents:

```bash
# Check API container recreate cadence
docker events --filter container=treadmill-api --since 10m \
  --format '{{.Time}} {{.Action}}'

# Check channel-server reconnect events (Bun process under each session)
# The events server polls the API; reconnects show in its log if it has one
journalctl --user -u 'treadmill-channel@*' --since '10 min ago' \
  | grep -iE 'reconnect|catch_up'
```

If the API was recreated multiple times, expect one re-emit per recreate.

## What should change durably

**Debounce or deduplicate the catch-up emission.** Two viable approaches:

1. **Hash-and-suppress**: the channel server hashes the snapshot
   contents on each emission and suppresses re-emission when the hash
   matches the previous one. Cheap state (one hash per session label),
   no semantic change.

2. **Emit-on-change**: track the underlying tasks/events state and only
   emit a catch-up snapshot when state actually changes. More invasive
   but cleaner — the catch-up message becomes a true "this is new
   state" signal.

Option 1 is the smaller surgical fix and probably what we want first.
Option 2 is the right end-state but waits on a redesign of the catch-up
contract.

Either way: the current behavior makes the catch-up signal useless as
a "did something change?" indicator, because the answer is "no" most
times the message fires.

## Sibling pattern

This is the same class as 2026-06-05's recurring-scope-spanning-test
miss: a signal that fires reliably (the channel emits the snapshot)
but doesn't carry the discrimination the consumer needs (the consumer
can't tell "this is new" from "this is the same as last time"). The
fix shape — adding the missing discrimination at the emitter — applies
to both.

## References

- ADR-0068: treadmill-events channel + shared channel conventions
- `tools/cc-channels/` — channel server implementation
- `feedback_no_implied_async_monitoring` — operator-side discipline
  about not creating watch-loops; if the channel deduplicates properly,
  the operator's reasoning load drops accordingly.
