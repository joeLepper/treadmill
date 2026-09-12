import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { mkdirpDurable } from './durable.mjs'

test('mkdirpDurable creates a fresh nested chain (each new dir durable up the chain)', () => {
  const root = mkdtempSync(join(tmpdir(), 'durable-'))
  try {
    const target = join(root, 'quarantine', 'treadmill-carla', 'sub')
    const ret = mkdirpDurable(target)
    assert.equal(ret, target)
    assert.ok(statSync(target).isDirectory())
    assert.ok(statSync(join(root, 'quarantine')).isDirectory(), 'intermediate parent created')
    assert.ok(statSync(join(root, 'quarantine', 'treadmill-carla')).isDirectory())
  } finally { rmSync(root, { recursive: true, force: true }) }
})

test('mkdirpDurable is idempotent on an existing chain', () => {
  const root = mkdtempSync(join(tmpdir(), 'durable-'))
  try {
    const target = join(root, 'a', 'b')
    mkdirpDurable(target)
    assert.doesNotThrow(() => mkdirpDurable(target)) // no re-create, no throw
    assert.ok(statSync(target).isDirectory())
  } finally { rmSync(root, { recursive: true, force: true }) }
})
