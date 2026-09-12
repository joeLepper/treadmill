# Inbound receiver (ADR-0102)

`inbound.mjs` uses Node builtins, including `node:sqlite` (tested on Node 25.2.1).
No package install is required. SQLite currently prints an experimental-feature
warning to stderr. It provides a process-safe lock, not the ledger storage format.

## Alan's interface

Persistent JSONL service, one process watching Fran's pinned thread:

```sh
node /home/joe/fran/bridge/inbound.mjs serve
```

Keep stdin open. Send one JSON object per line:

```json
{"id":"bus-request-1","method":"deliver","params":{"dedupKey":"bus-event-123","from":"alan","text":"Please review the diff."}}
```

Stdout contains typed JSON objects only:

```json
{"type":"result","id":"bus-request-1","result":{"dedupKey":"bus-event-123","state":"submitted","enqueued":true,"commit":null}}
{"type":"committed","dedupKey":"bus-event-123","commitId":"stable-sha256","threadId":"uuid","turnId":"uuid","ts":"ISO timestamp"}
```

A submission result is **not** permission to acknowledge the bus. Consume the
`committed` event. Its `commitId` is stable for `(threadId, dedupKey)`.
Errors have `type:"error"`, the request `id`, and an `error` string. Diagnostics
and watcher errors go to stderr. Watcher errors never commit an unknown outcome.

Other service methods: `status` with `{dedupKey}`, `commits` without params,
and the explicit recovery method `confirm-absent` described below. Response IDs
are correlation only, not message deduplication keys.

Function interface:

```js
import { createInbound, deliver } from './inbound.mjs';
// Convenience call using the default pinned thread and ledger:
await deliver({ dedupKey, from, text });

// A long-lived receiver when the caller also owns completion observation:
const receiver = createInbound();
receiver.on('committed', event => ackBusIdempotently(event));
receiver.on('watchError', error => reportFailure(error));
const stop = receiver.watch();
await receiver.deliver({ dedupKey, from, text });
// On caller restart, recover ack notifications from receiver.commits().
```

Convenience `deliver()` does not start a watcher. Use `serve`, a separate `watch`
process, or the long-lived function interface above. Service EOF stops watching;
the durable records remain for the next process.

One-shot CLI commands read a JSON object on stdin where indicated:

- `deliver`: `{dedupKey,from,text}`; submits or returns existing state.
- `status`: `{dedupKey}`; returns the full stored record.
- `scan`: reconcile the current rollout once.
- `commits`: print every durable committed event as JSONL.
- `watch`: replay existing commits, then poll and report new ones.

Optional flags: `--ledger-dir PATH`, `--thread UUID`, `--rollout PATH`.
Defaults: `~/fran/bridge/ledger`, UUID in `~/fran/.session-uuid`, and the unique
matching JSONL rollout under `~/.codex/sessions`. The ledger is bound to a thread;
reuse with a different thread fails closed. `serve`/`watch` poll every second.

## Durable contract

One `<sha256(dedupKey)>.json` file holds the message, state, correlation token,
attempt metadata, associated turn IDs, and (when completed) the commit event.
Each update writes an exclusive temporary file, fsyncs it, renames it over the
record, and fsyncs the ledger directory. `.tmp` files left by a crash are ignored.
SQLite's local process lock serializes updates across receiver processes and
releases on process death. Use a local filesystem, not an NFS ledger directory.

- New: persist `submitted` **before** calling `codex queue`.
- Submitted/running: redelivery is a no-op, regardless of elapsed time.
- Completed: redelivery is a no-op and returns the existing commit.
- Confirmed-absent: redelivery persists a new submitted attempt and re-enqueues.
- Reusing a key for different text/from, malformed records, and wrong-thread
  rollout data fail closed.

**Exactly once means one durable logical commit.** The commit event is inside
the completed record, not a second file. A process can die after committing but
before emitting stdout/calling a listener. `commits()`/the CLI and service startup
replay the same stable events. Alan's bus acknowledgment must be idempotent by
key; notification transmission itself is at-least-once. Ledger records must not
be deleted while bus redelivery remains possible.

The bridge permits duplicate execution. It does not roll back file writes or
deduplicate outbound effects. That is the settled ADR's at-least-once scope.

## Submission and exact completion observable

Submission executes `codex queue --thread <UUID> --message <envelope>` without a
shell. It has a 30-second timeout. A timeout/error retains the submitted record:
the queue may have accepted it. CLI success records acceptance time, not completion.
The message contains `FRAN_INBOUND_V1` followed by a JSON object with the full
`dedupKey`, `from`, `text`, and a random per-record correlation token.

The read-only rollout adapter is pinned to **Codex 0.154.0**:

1. Verify `session_meta.payload.id` equals the pinned thread UUID.
2. `event_msg` / `payload.type:"task_started"` establishes a `turn_id`.
3. A `response_item` user message whose `input_text` **exactly equals** the stored
   envelope binds that turn to the key. Quoted assistant text never binds a key.
4. `event_msg` / **`payload.type:"task_complete"`** with that same `turn_id` commits
   it. `turn_aborted` is not completion. Multiple keys merged into one turn fail
   closed, rather than silently acknowledging them as independent turns.

Full snapshot scans recover association after an accept-before-record crash and
recover completions missed while the watcher was down. Only complete JSONL lines
are consumed. This adapter rereads the rollout each poll; it favors simple crash
recovery over large-history efficiency. A missing/replaced/corrupted rollout is
an error, not proof that a queued message is absent.

The app-server also provides `turn/completed` with `turn.status` (only
`completed` is success), but this implementation does **not** subscribe to that
transport. Its Unix control socket requires WebSocket framing and an HTTP
Upgrade handshake. Native `clientUserMessageId` idempotency and pending-delete
fencing semantics are unproven; neither is used as a correctness assumption.
The daemon is never started, stopped, restarted, or reconfigured by this module.

## Explicit recovery, never timeout-based absence

After investigating a lost/aborted submission, Alan/operator may call:

```json
{"id":"recover-1","method":"confirm-absent","params":{"dedupKey":"bus-event-123","evidence":"Operator inspected the stopped queue and authorized replay","acceptDuplicateRisk":true}}
```

Then redeliver the **same** `{dedupKey,from,text}`. The method records the recovery
decision; it does not pretend to prove that a delayed submission cannot arrive.
It never downgrades a completed record. Historical start events do not undo this
decision; discovery of a new delayed turn adopts it. A crash between intent and
queue invocation needs this recovery path too. Automatic retry of a submitted
key is deliberately absent.

## Tests and limits

Run `node --test /home/joe/fran/bridge/inbound.test.mjs`.
Tests use isolated ledgers, fake queue acceptance, realistic rollout events,
actual child-process exits/restarts, and concurrent receiver processes. They
never submit a live message or restart the shared daemon. The real rollout can
be inspected read-only with `scan` and an empty isolated ledger.

Live queue-to-envelope fidelity and daemon restart survival still need a controlled
integration foil. A Codex version or rollout-format change must revalidate this
adapter before using its commits to acknowledge the bus. This module does not
provide bus FIFO scheduling; Alan's caller owns ordered delivery and retry policy.
