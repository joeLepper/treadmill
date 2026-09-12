import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, readdirSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { assertLabel, relayDir, formatBody, injectToRelay, isWatcherEligible } from './inject.mjs'

test('assertLabel accepts a plain label', () => {
  assert.equal(assertLabel('treadmill-carla'), 'treadmill-carla')
})

test('assertLabel rejects traversal and separators (path-safety foil)', () => {
  for (const bad of ['..', '../x', 'a/b', '/abs', '', '.', 'a\0b', 'x'.repeat(200)]) {
    assert.throws(() => assertLabel(bad), /invalid session label/, `should reject ${JSON.stringify(bad)}`)
  }
})

test('relayDir stays under the root', () => {
  const root = '/tmp/ccroot'
  assert.equal(relayDir('treadmill-carla', root), '/tmp/ccroot/treadmill-carla/relay')
})

test('formatBody carries the text and a telegram header', () => {
  const body = formatBody({ text: 'hello world', from: '@joe', chatId: 42, updateId: 7, date: 0 })
  assert.match(body, /telegram inbound via bridge/)
  assert.match(body, /chat_id=42 update_id=7/)
  assert.match(body, /hello world/)
})

test('injectToRelay writes a complete .md into the relay dir, no tmp left', () => {
  const root = mkdtempSync(join(tmpdir(), 'ccroot-'))
  try {
    const p = injectToRelay(
      { label: 'treadmill-carla', text: 'ping from phone', from: '@joe', chatId: 42, updateId: 7, date: 0 },
      root,
    )
    const dir = join(root, 'treadmill-carla', 'relay')
    const files = readdirSync(dir)
    assert.equal(files.length, 1, 'exactly one file, no leftover .tmp')
    assert.ok(files[0].endsWith('.md'), 'file is a .md the channel server will pick up')
    assert.equal(p, join(dir, files[0]))
    assert.match(readFileSync(p, 'utf8'), /ping from phone/)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test('injectToRelay refuses a crafted label (no escape via inject)', () => {
  const root = mkdtempSync(join(tmpdir(), 'ccroot-'))
  try {
    assert.throws(
      () => injectToRelay({ label: '../evil', text: 'x', chatId: 1, updateId: 1 }, root),
      /invalid session label/,
    )
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test('injectToRelay refuses a symlinked relay dir (no delivery into another session)', () => {
  const root = mkdtempSync(join(tmpdir(), 'ccroot-'))
  try {
    // treadmill-carla/relay is a symlink to treadmill-alan/relay — the
    // configured-label -> wrong-session foil.
    mkdirSync(join(root, 'treadmill-carla'), { recursive: true })
    mkdirSync(join(root, 'treadmill-alan', 'relay'), { recursive: true })
    symlinkSync(join(root, 'treadmill-alan', 'relay'), join(root, 'treadmill-carla', 'relay'))
    assert.throws(
      () => injectToRelay({ label: 'treadmill-carla', text: 'x', chatId: 1, updateId: 1 }, root),
      /symlink|does not resolve/,
    )
    assert.equal(readdirSync(join(root, 'treadmill-alan', 'relay')).length, 0, 'nothing leaked into alan')
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})

test('isWatcherEligible is true only for a launcher session (session-id record present)', () => {
  const root = mkdtempSync(join(tmpdir(), 'ccroot-'))
  try {
    mkdirSync(join(root, 'treadmill-carla'), { recursive: true })
    assert.equal(isWatcherEligible('treadmill-carla', root), false, 'no session-id -> ineligible')
    writeFileSync(join(root, 'treadmill-carla', 'session-id'), 'uuid\n')
    assert.equal(isWatcherEligible('treadmill-carla', root), true)
    assert.equal(isWatcherEligible('nonexistent', root), false)
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})
