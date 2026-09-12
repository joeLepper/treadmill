import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { parseConfig, loadToken, loadBots } from './config.mjs'

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

function seedSession(root, label, { eligible, token = '9:tok' } = {}) {
  mkdirSync(join(root, label), { recursive: true })
  if (eligible) writeFileSync(join(root, label, 'session-id'), 'uuid\n')
  writeFileSync(join(root, label, 'telegram.env'), `TELEGRAM_BOT_TOKEN=${token}\n`)
}

test('loadBots loads eligible launcher sessions with their tokens', () => {
  const root = mkdtempSync(join(tmpdir(), 'ccroot-'))
  const cfg = join(root, 'bots.json')
  try {
    seedSession(root, 'treadmill-carla', { eligible: true, token: 'AA:11' })
    writeFileSync(cfg, JSON.stringify({ bots: [{ label: 'treadmill-carla', allowedChats: [42] }] }))
    const bots = loadBots(cfg, root)
    assert.equal(bots[0].token, 'AA:11')
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
