import { test } from 'node:test'
import assert from 'node:assert/strict'
import { parseRouting, resolveLabel } from './routing.mjs'

test('parseRouting builds a chat_id -> label table', () => {
  const t = parseRouting({ routes: { '8956818786': 'treadmill-carla', '-100200': 'treadmill-alan' } })
  assert.equal(t.get('8956818786'), 'treadmill-carla')
  assert.equal(t.get('-100200'), 'treadmill-alan')
})

test('parseRouting rejects a non-integer chat_id key', () => {
  assert.throws(() => parseRouting({ routes: { 'not-a-number': 'treadmill-carla' } }), /chat_id must be an integer/)
})

test('parseRouting rejects an unsafe label', () => {
  assert.throws(() => parseRouting({ routes: { '42': '../evil' } }), /invalid session label/)
})

test('parseRouting rejects a missing routes object', () => {
  assert.throws(() => parseRouting({}), /must have a "routes" object/)
  assert.throws(() => parseRouting(null), /must be an object/)
})

test('resolveLabel returns null for an unmapped chat (fail-closed)', () => {
  const t = parseRouting({ routes: { '42': 'treadmill-carla' } })
  assert.equal(resolveLabel(t, 42), 'treadmill-carla')
  assert.equal(resolveLabel(t, '42'), 'treadmill-carla')
  assert.equal(resolveLabel(t, 99), null)
})
