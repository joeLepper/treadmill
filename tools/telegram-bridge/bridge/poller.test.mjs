import { test } from 'node:test'
import assert from 'node:assert/strict'
import { pollOnce, runBot, runDaemon, ConflictError } from './poller.mjs'

// In-memory ledger with the same contract as ledger.mjs.
function fakeLedger() {
  return {
    offset: 0, seen: new Set(), injected: [], advanced: [],
    has(id) { return this.seen.has(id) },
    advance(id) { this.advanced.push(id); if (id + 1 > this.offset) this.offset = id + 1 },
    commit(id) { this.injected.push(id); this.seen.add(id); if (id + 1 > this.offset) this.offset = id + 1 },
  }
}

const fetchReturning = (body, { ok = true, status = 200 } = {}) => async () => ({ ok, status, json: async () => body })
const msg = (update_id, chatId, text, extra = {}) => ({ update_id, message: { chat: { id: chatId }, text, date: 0, ...extra } })

function harness(body, opts = {}) {
  const injects = []
  const quarantined = []
  const ledger = opts.ledger ?? fakeLedger()
  const deps = {
    token: 'T',
    label: 'treadmill-carla',
    allowedChats: new Set(['42']),
    fetchFn: opts.fetchFn ?? fetchReturning(body),
    inject: opts.inject ?? (m => injects.push(m)),
    quarantine: opts.quarantine ?? ((u, e) => quarantined.push({ u, e })),
    ledger,
    log: () => {},
  }
  return { injects, quarantined, ledger, deps }
}

test('injects an allowed text message to its fixed label and advances', async () => {
  const { injects, ledger, deps } = harness({ ok: true, result: [msg(100, 42, 'hi', { from: { username: 'joe' } })] })
  const r = await pollOnce(deps)
  assert.equal(r.injected, 1)
  assert.equal(injects[0].label, 'treadmill-carla')
  assert.equal(injects[0].text, 'hi')
  assert.equal(injects[0].from, '@joe')
  assert.deepEqual(ledger.injected, [100])
  assert.equal(ledger.offset, 101)
})

test('drops a chat not in the allowlist (fail-closed), never injects', async () => {
  const { injects, ledger, deps } = harness({ ok: true, result: [msg(5, 999, 'who am I')] })
  const r = await pollOnce(deps)
  assert.equal(r.dropped, 1)
  assert.equal(injects.length, 0)
  assert.deepEqual(ledger.advanced, [5])
})

test('at-least-once dedup: an already-seen update is skipped, not re-injected', async () => {
  const { injects, ledger, deps } = harness({ ok: true, result: [msg(100, 42, 'dup')] })
  ledger.seen.add(100)
  const r = await pollOnce(deps)
  assert.equal(r.injected, 0)
  assert.equal(injects.length, 0)
  assert.deepEqual(ledger.advanced, [100])
})

test('a non-text update is dropped and advanced', async () => {
  const { injects, ledger, deps } = harness({ ok: true, result: [{ update_id: 8, edited_message: { chat: { id: 42 }, text: 'e' } }] })
  const r = await pollOnce(deps)
  assert.equal(r.dropped, 1)
  assert.equal(injects.length, 0)
  assert.deepEqual(ledger.advanced, [8])
})

test('a poison update does NOT stall the batch (head-of-line): quarantined + advanced, later update still delivered', async () => {
  let n = 0
  const injects = []
  const { quarantined, ledger, deps } = harness(
    { ok: true, result: [msg(10, 42, 'poison'), msg(11, 42, 'good')] },
    { inject: m => { n++; if (n === 1) throw new Error('disk full'); injects.push(m) } },
  )
  const r = await pollOnce(deps)
  assert.equal(r.quarantined, 1)
  assert.equal(r.injected, 1)
  assert.equal(quarantined.length, 1)
  assert.equal(quarantined[0].u.update_id, 10)
  assert.deepEqual(ledger.advanced, [10]) // poison advanced past
  assert.deepEqual(ledger.injected, [11]) // good still delivered
  assert.equal(injects[0].text, 'good')
})

test('an HTTP error throws so the caller backs off', async () => {
  const { deps } = harness(null, { fetchFn: fetchReturning({}, { ok: false, status: 502 }) })
  await assert.rejects(() => pollOnce(deps), /HTTP 502/)
})

test('a non-409 Bot-API error throws a generic error', async () => {
  const { deps } = harness({ ok: false, description: 'Bad Request' })
  await assert.rejects(() => pollOnce(deps), /not ok/)
})

test('an HTTP-409 status throws ConflictError (classified by status, before the generic throw)', async () => {
  const { deps } = harness(null, { fetchFn: fetchReturning({}, { ok: false, status: 409 }) })
  await assert.rejects(() => pollOnce(deps), ConflictError)
})

test('a body-level 409 (HTTP 200, ok:false error_code 409) also throws ConflictError', async () => {
  const { deps } = harness({ ok: false, error_code: 409, description: 'terminated by other getUpdates request' })
  await assert.rejects(() => pollOnce(deps), ConflictError)
})

test('NO DATA LOSS: a failed inject AND a failed quarantine does not advance the offset', async () => {
  const { ledger, deps } = harness(
    { ok: true, result: [msg(70, 42, 'poison')] },
    { inject: () => { throw new Error('inject boom') }, quarantine: () => { throw new Error('disk full') } },
  )
  await assert.rejects(() => pollOnce(deps), /disk full/)
  assert.deepEqual(ledger.advanced, [], 'offset must NOT advance when neither delivery nor quarantine succeeded')
  assert.deepEqual(ledger.injected, [])
  assert.equal(ledger.offset, 0)
})

test('a delivered message whose ledger write then fails is NOT quarantined (offset retained, retried)', async () => {
  const ledger = fakeLedger()
  ledger.commit = () => { throw new Error('ledger write failed') }
  const injects = []
  const { quarantined, deps } = harness(
    { ok: true, result: [msg(80, 42, 'delivered')] },
    { ledger, inject: m => injects.push(m) },
  )
  await assert.rejects(() => pollOnce(deps), /ledger write failed/)
  assert.equal(injects.length, 1, 'it WAS delivered')
  assert.equal(quarantined.length, 0, 'a delivered message must not be quarantined')
  assert.equal(ledger.offset, 0, 'offset retained → retries (at-least-once duplicate on retry)')
})

test('runBot backs off on error then stops cleanly', async () => {
  let calls = 0
  const sleeps = []
  const { stop, done } = runBot({
    token: 'T', label: 'treadmill-carla', allowedChats: new Set(),
    fetchFn: async () => { calls++; throw new Error('boom') },
    inject: () => {}, ledger: fakeLedger(),
    minBackoffMs: 1, maxBackoffMs: 4,
    sleep: async ms => { sleeps.push(ms); if (sleeps.length >= 3) stop() },
    log: () => {},
  })
  await done
  assert.ok(calls >= 3)
  assert.deepEqual(sleeps.slice(0, 3), [1, 2, 4])
})

test('runDaemon supervises multiple bots and stop() stops all', async () => {
  const seen = new Set()
  const { stop, done } = runDaemon({
    fetchFn: async url => { seen.add(url.includes('botA') ? 'A' : 'B'); throw new Error('x') },
    inject: () => {}, minBackoffMs: 1, maxBackoffMs: 1,
    sleep: async () => { if (seen.size >= 2) stop() },
    log: () => {},
    bots: [
      { token: 'botA', label: 'treadmill-alan', allowedChats: new Set(), ledger: fakeLedger() },
      { token: 'botB', label: 'treadmill-carla', allowedChats: new Set(), ledger: fakeLedger() },
    ],
  })
  await done
  assert.deepEqual([...seen].sort(), ['A', 'B'])
})
