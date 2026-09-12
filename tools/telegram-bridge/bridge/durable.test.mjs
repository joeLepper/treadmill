import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { mkdirpDurable } from './durable.mjs'

test('mkdirpDurable creates a fresh nested chain below the anchor', () => {
  const root = mkdtempSync(join(tmpdir(), 'durable-'))
  try {
    const target = join(root, 'quarantine', 'treadmill-carla', 'sub')
    assert.equal(mkdirpDurable(target, root), target)
    assert.ok(statSync(target).isDirectory())
    assert.ok(statSync(join(root, 'quarantine')).isDirectory())
  } finally { rmSync(root, { recursive: true, force: true }) }
})

test('mkdirpDurable is idempotent on an existing chain', () => {
  const root = mkdtempSync(join(tmpdir(), 'durable-'))
  try {
    const target = join(root, 'a', 'b')
    mkdirpDurable(target, root)
    assert.doesNotThrow(() => mkdirpDurable(target, root))
    assert.ok(statSync(target).isDirectory())
  } finally { rmSync(root, { recursive: true, force: true }) }
})

// Mutation-sensitive: fails if the parent fsync is dropped.
test('fsyncs the parent of every component, in shallow→deep order', () => {
  const fsynced = []
  const made = []
  // anchor exists; components are missing.
  const ops = { mkdir: d => made.push(d), fsyncDir: d => fsynced.push(d), statDir: p => (p === '/anchor' ? 'dir' : 'missing') }
  mkdirpDurable('/anchor/x/y/z', '/anchor', ops)
  assert.deepEqual(made, ['/anchor/x', '/anchor/x/y', '/anchor/x/y/z'])
  assert.deepEqual(fsynced, ['/anchor', '/anchor/x', '/anchor/x/y'], 'each new dir fsyncs its parent')
})

// Fran's exact foil: a retry where every dir already EXISTS must STILL fsync
// (a prior run may have created the dir but crashed before the parent fsync).
test('RETRY: existing dirs are re-fsynced (exists() is NOT treated as durable)', () => {
  const fsynced = []
  const made = []
  const ops = { mkdir: d => made.push(d), fsyncDir: d => fsynced.push(d), statDir: () => 'dir' }
  mkdirpDurable('/anchor/x/y', '/anchor', ops)
  assert.deepEqual(made, [], 'nothing created — all exist')
  assert.ok(fsynced.length > 0, 'must still fsync on the retry path')
  assert.deepEqual(fsynced, ['/anchor', '/anchor/x'])
})

test('rejects an anchor that is not an ancestor, and a non-directory component', () => {
  const dirEverywhere = { mkdir: () => {}, fsyncDir: () => {}, statDir: () => 'dir' }
  assert.throws(() => mkdirpDurable('/other/x', '/anchor', dirEverywhere), /not an ancestor/)
  const fileComponent = { mkdir: () => {}, fsyncDir: () => {}, statDir: p => (p === '/anchor' ? 'dir' : 'notdir') }
  assert.throws(() => mkdirpDurable('/anchor/x', '/anchor', fileComponent), /not a directory/)
})
