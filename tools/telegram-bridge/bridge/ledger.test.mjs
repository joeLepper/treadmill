import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { Ledger } from './ledger.mjs'

function withDir(fn) {
  const dir = mkdtempSync(join(tmpdir(), 'tgledger-'))
  try { return fn(join(dir, 'ledger.json')) } finally { rmSync(dir, { recursive: true, force: true }) }
}

test('fresh ledger starts at offset 0', () => {
  withDir(path => { assert.equal(new Ledger(path).offset, 0) })
})

test('commit marks seen and advances the ack to updateId+1', () => {
  withDir(path => {
    const l = new Ledger(path)
    l.commit(10)
    assert.equal(l.offset, 11)
    assert.ok(l.has(10))
    assert.ok(!l.has(9))
  })
})

test('advance moves the offset but does not mark seen', () => {
  withDir(path => {
    const l = new Ledger(path)
    l.advance(5)
    assert.equal(l.offset, 6)
    assert.ok(!l.has(5))
  })
})

test('state survives a reload (durable offset + seen)', () => {
  withDir(path => {
    const a = new Ledger(path)
    a.commit(20)
    a.advance(21)
    const b = new Ledger(path)
    assert.equal(b.offset, 22)
    assert.ok(b.has(20))
    assert.ok(!b.has(21))
  })
})

test('offset only moves forward', () => {
  withDir(path => {
    const l = new Ledger(path)
    l.commit(100)
    l.advance(3) // stale/out-of-order update must not rewind the ack
    assert.equal(l.offset, 101)
  })
})

test('seen set is bounded (cannot grow without limit)', () => {
  withDir(path => {
    const l = new Ledger(path)
    for (let i = 0; i < 1500; i++) l.commit(i)
    assert.ok(l.seen.length <= 1000)
    assert.ok(l.has(1499), 'recent update remembered')
    assert.ok(!l.has(0), 'oldest update evicted')
  })
})
