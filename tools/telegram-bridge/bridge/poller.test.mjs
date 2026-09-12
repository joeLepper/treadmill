import { test } from 'node:test'
import assert from 'node:assert/strict'
import { pollOnce, runPoller } from './poller.mjs'

// A minimal in-memory ledger with the same contract as ledger.mjs.
function fakeLedger() {
  return {
    offset: 0, seen: new Set(), injected: [], advanced: [],
    has(id) { return this.seen.has(id) },
    advance(id) { this.advanced.push(id); if (id + 1 > this.offset) this.offset = id + 1 },
    commit(id) { this.injected.push(id); this.seen.add(id); if (id + 1 > this.offset) this.offset = id + 1 },
  }
}

function fetchReturning(body, { ok = true, status = 200 } = {}) {
  return async () => ({ ok, status, json: async () => body })
}

const routes = { 42: 'treadmill-carla', 7: 'treadmill-alan' }
const resolveLabel = id => routes[id] ?? null

function harness(body, opts = {}) {
  const injects = []
  const ledger = fakeLedger()
  const deps = {
    token: 'T',
    fetchFn: opts.fetchFn ?? fetchReturning(body),
    inject: m => injects.push(m),
    resolveLabel,
    ledger,
    log: () => {},
  }
  return { injects, ledger, deps }
}

test('injects a mapped text message and advances the ack', async () => {
  const { injects, ledger, deps } = harness({
    ok: true,
    result: [{ update_id: 100, message: { chat: { id: 42 }, from: { username: 'joe' }, text: 'hi', date: 0 } }],
  })
  const n = await pollOnce(deps)
  assert.equal(n, 1)
  assert.equal(injects.length, 1)
  assert.equal(injects[0].label, 'treadmill-carla')
  assert.equal(injects[0].text, 'hi')
  assert.equal(injects[0].from, '@joe')
  assert.deepEqual(ledger.injected, [100])
  assert.equal(ledger.offset, 101)
})

test('drops an unmapped chat (fail-closed) — advanced, never delivered', async () => {
  const { injects, ledger, deps } = harness({
    ok: true,
    result: [{ update_id: 5, message: { chat: { id: 999 }, text: 'who am I', date: 0 } }],
  })
  const n = await pollOnce(deps)
  assert.equal(n, 0)
  assert.equal(injects.length, 0)
  assert.deepEqual(ledger.advanced, [5])
  assert.equal(ledger.offset, 6)
})

test('effectively-once: an already-seen update is skipped, not re-injected', async () => {
  const { injects, ledger, deps } = harness({
    ok: true,
    result: [{ update_id: 100, message: { chat: { id: 42 }, text: 'dup', date: 0 } }],
  })
  ledger.seen.add(100)
  const n = await pollOnce(deps)
  assert.equal(n, 0)
  assert.equal(injects.length, 0, 'must not re-inject a seen update')
  assert.deepEqual(ledger.advanced, [100])
})

test('a non-text update is advanced, not injected', async () => {
  const { injects, ledger, deps } = harness({
    ok: true,
    result: [{ update_id: 8, edited_message: { chat: { id: 42 }, text: 'edit', date: 0 } }],
  })
  const n = await pollOnce(deps)
  assert.equal(n, 0)
  assert.equal(injects.length, 0)
  assert.deepEqual(ledger.advanced, [8])
})

test('mixed batch: mapped injected, unmapped dropped, offset ends past the max', async () => {
  const { injects, ledger, deps } = harness({
    ok: true,
    result: [
      { update_id: 10, message: { chat: { id: 42 }, text: 'to carla', date: 0 } },
      { update_id: 11, message: { chat: { id: 999 }, text: 'nowhere', date: 0 } },
      { update_id: 12, message: { chat: { id: 7 }, text: 'to alan', date: 0 } },
    ],
  })
  await pollOnce(deps)
  assert.deepEqual(injects.map(i => i.label), ['treadmill-carla', 'treadmill-alan'])
  assert.deepEqual(ledger.injected, [10, 12])
  assert.deepEqual(ledger.advanced, [11])
  assert.equal(ledger.offset, 13)
})

test('an HTTP error throws so the caller backs off', async () => {
  const { deps } = harness(null, { fetchFn: fetchReturning({}, { ok: false, status: 502 }) })
  await assert.rejects(() => pollOnce(deps), /HTTP 502/)
})

test('a Bot-API not-ok body throws', async () => {
  const { deps } = harness({ ok: false, description: 'Conflict: terminated by other getUpdates request' })
  await assert.rejects(() => pollOnce(deps), /not ok/)
})

test('runPoller backs off on error then stops cleanly', async () => {
  let calls = 0
  const sleeps = []
  const ledger = fakeLedger()
  const { stop, done } = runPoller({
    token: 'T',
    fetchFn: async () => { calls++; throw new Error('boom') },
    inject: () => {},
    resolveLabel,
    ledger,
    minBackoffMs: 1,
    maxBackoffMs: 4,
    sleep: async ms => { sleeps.push(ms); if (sleeps.length >= 3) stop() },
    log: () => {},
  })
  await done
  assert.ok(calls >= 3, 'kept polling across errors')
  assert.deepEqual(sleeps.slice(0, 3), [1, 2, 4], 'capped exponential backoff')
})
