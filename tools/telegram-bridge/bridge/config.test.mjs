import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { parseConfig, loadToken, loadBots, botIdOf } from './config.mjs'

test('parseConfig returns labels with allowedChats as string Sets', () => {
  const bots = parseConfig({ bots: [{ label: 'treadmill-carla', allowedChats: [42, -100] }] })
  assert.equal(bots[0].label, 'treadmill-carla')
  assert.ok(bots[0].allowedChats.has('42'))
  assert.ok(bots[0].allowedChats.has('-100'))
})

test('parseConfig rejects empty/missing bots, bad label, dup, empty allowlist, non-int chat', () => {
  assert.throws(() => parseConfig({}), /non-empty "bots"/)
  assert.throws(() => parseConfig({ bots: [] }), /non-empty "bots"/)
  assert.throws(() => parseConfig({ bots: [{ label: '../evil', allowedChats: [1] }] }), /invalid session label/)
  assert.throws(() => parseConfig({ bots: [{ label: 'a', allowedChats: [1] }, { label: 'a', allowedChats: [2] }] }), /duplicate bot label/)
  assert.throws(() => parseConfig({ bots: [{ label: 'a', allowedChats: [] }] }), /non-empty "allowedChats"/)
  assert.throws(() => parseConfig({ bots: [{ label: 'a', allowedChats: ['x'] }] }), /must be integers/)
})

test('loadToken reads TELEGRAM_BOT_TOKEN and strips quotes', () => {
  const root = mkdtempSync(join(tmpdir(), 'ccroot-'))
  try {
    mkdirSync(join(root, 'treadmill-carla'), { recursive: true })
    writeFileSync(join(root, 'treadmill-carla', 'telegram.env'), 'TELEGRAM_BOT_TOKEN="123:abc"\n')
    assert.equal(loadToken('treadmill-carla', root), '123:abc')
  } finally { rmSync(root, { recursive: true, force: true }) }
})

test('loadToken throws when no token present', () => {
  const root = mkdtempSync(join(tmpdir(), 'ccroot-'))
  try {
    mkdirSync(join(root, 'treadmill-carla'), { recursive: true })
    writeFileSync(join(root, 'treadmill-carla', 'telegram.env'), 'OTHER=1\n')
    assert.throws(() => loadToken('treadmill-carla', root), /no TELEGRAM_BOT_TOKEN/)
  } finally { rmSync(root, { recursive: true, force: true }) }
})

function seedSession(root, label, { eligible, bridged = true, token = '9:tok' } = {}) {
  mkdirSync(join(root, label), { recursive: true })
  if (eligible) writeFileSync(join(root, label, 'session-id'), 'uuid\n')
  if (bridged) writeFileSync(join(root, label, 'telegram-bridged'), '') // ADR-0106 cutover marker
  writeFileSync(join(root, label, 'telegram.env'), `TELEGRAM_BOT_TOKEN=${token}\n`)
}

test('loadBots loads eligible launcher sessions with their tokens', () => {
  const root = mkdtempSync(join(tmpdir(), 'ccroot-'))
  const cfg = join(root, 'bots.json')
  try {
    seedSession(root, 'treadmill-carla', { eligible: true, token: '11:AAtok' })
    writeFileSync(cfg, JSON.stringify({ bots: [{ label: 'treadmill-carla', allowedChats: [42] }] }))
    const bots = loadBots(cfg, root)
    assert.equal(bots[0].token, '11:AAtok')
    assert.equal(bots[0].botId, '11')
    assert.ok(bots[0].allowedChats.has('42'))
  } finally { rmSync(root, { recursive: true, force: true }) }
})

test('loadBots refuses a non-launcher (pure-fabric/unknown) label — no session-id record', () => {
  const root = mkdtempSync(join(tmpdir(), 'ccroot-'))
  const cfg = join(root, 'bots.json')
  try {
    seedSession(root, 'treadmill-carla', { eligible: false })
    writeFileSync(cfg, JSON.stringify({ bots: [{ label: 'treadmill-carla', allowedChats: [42] }] }))
    assert.throws(() => loadBots(cfg, root), /not a launcher-managed session/)
  } finally { rmSync(root, { recursive: true, force: true }) }
})

test('loadBots refuses a label with no telegram-bridged marker (its session still polls → 409 guard)', () => {
  const root = mkdtempSync(join(tmpdir(), 'ccroot-'))
  const cfg = join(root, 'bots.json')
  try {
    seedSession(root, 'treadmill-carla', { eligible: true, bridged: false })
    writeFileSync(cfg, JSON.stringify({ bots: [{ label: 'treadmill-carla', allowedChats: [42] }] }))
    assert.throws(() => loadBots(cfg, root), /no telegram-bridged marker/)
  } finally { rmSync(root, { recursive: true, force: true }) }
})

test('loadBots refuses two labels sharing a token (no two pollers on one bot), and does NOT leak the token', () => {
  const root = mkdtempSync(join(tmpdir(), 'ccroot-'))
  const cfg = join(root, 'bots.json')
  const token = '77:supersecret'
  try {
    seedSession(root, 'treadmill-carla', { eligible: true, token })
    seedSession(root, 'treadmill-alan', { eligible: true, token })
    writeFileSync(cfg, JSON.stringify({ bots: [
      { label: 'treadmill-carla', allowedChats: [42] },
      { label: 'treadmill-alan', allowedChats: [42] },
    ] }))
    let msg = ''
    assert.throws(() => loadBots(cfg, root), e => { msg = e.message; return /share bot id 77/.test(e.message) })
    assert.ok(!msg.includes('supersecret'), 'the error must not print the token secret')
  } finally { rmSync(root, { recursive: true, force: true }) }
})

test('loadToken strips a trailing inline comment on an unquoted value', () => {
  const root = mkdtempSync(join(tmpdir(), 'ccroot-'))
  try {
    mkdirSync(join(root, 'treadmill-carla'), { recursive: true })
    writeFileSync(join(root, 'treadmill-carla', 'telegram.env'), 'TELEGRAM_BOT_TOKEN=123:abc # primary bot\n')
    assert.equal(loadToken('treadmill-carla', root), '123:abc')
  } finally { rmSync(root, { recursive: true, force: true }) }
})

test('botIdOf extracts the public bot id and rejects a malformed token', () => {
  assert.equal(botIdOf('12345:AAHresttoken'), '12345')
  assert.throws(() => botIdOf('not-a-token'), /botId/)
})
