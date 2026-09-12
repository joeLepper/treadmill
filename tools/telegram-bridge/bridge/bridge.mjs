#!/usr/bin/env node
// bridge.mjs — the Telegram bridge daemon entrypoint (ADR-0106, multi-token).
// One process owns all configured per-session bots, polls each, and injects
// inbound into each session's relay dir. Run under systemd-user (see
// systemd/telegram-bridge.service).
//
//   node bridge.mjs
//
// Env:
//   TG_BRIDGE_STATE_DIR  state dir (default ~/.cc-channels/.telegram-bridge)
//   CC_CHANNELS_ROOT     relay + telegram.env root (default ~/.cc-channels)
//   TG_BRIDGE_LONGPOLL   long-poll seconds (default 25)
//
// Tokens are NOT passed here — each bot's token is read from
// ~/.cc-channels/<label>/telegram.env. Config lists {label, allowedChats}.

import { closeSync, fsyncSync, openSync, realpathSync, renameSync, unlinkSync, writeFileSync } from 'node:fs'
import { createServer } from 'node:net'
import { join } from 'node:path'
import { homedir } from 'node:os'
import { randomUUID } from 'node:crypto'
import { injectToRelay } from './inject.mjs'
import { loadBots } from './config.mjs'
import { Ledger } from './ledger.mjs'
import { runDaemon } from './poller.mjs'
import { mkdirpDurable } from './durable.mjs'

// Single-instance lock via a Linux ABSTRACT-namespace unix socket (Fran + Bert
// converge): "sole poller" must be kernel-enforced, not a pidfile. A pidfile
// has a create-then-write race (an empty file looks stale) and a pid-reuse race
// (a recycled pid reads as alive). An abstract socket has neither: the name is
// held only while this process lives and the kernel releases it on death (no
// file to go stale, no pid to parse). A second instance gets EADDRINUSE.
function acquireLock(name) {
  return new Promise((resolve, reject) => {
    const server = createServer()
    server.once('error', e => reject(e.code === 'EADDRINUSE'
      ? new Error('another telegram-bridge is already running (lock held)') : e))
    server.listen(`\0${name}`, () => resolve(() => server.close()))
  })
}

// Canonical, per-UID lock name so two different SPELLINGS of the same state dir
// (a symlink, a trailing slash) resolve to ONE lock (Bert/Fran nit).
function lockName(stateDir) {
  const uid = typeof process.getuid === 'function' ? process.getuid() : 'nouid'
  return `telegram-bridge:${uid}:${realpathSync(stateDir)}`
}

// Durable quarantine (Fran finding): a failed inject is written to disk with
// atomic rename + fsync BEFORE the offset advances, and this THROWS on failure
// so the caller retains the offset and retries — a thrown quarantine is never
// counted as handled, and a fsynced ledger advance can never outlive a
// non-durable quarantine copy after a machine crash.
function durableQuarantine(qdir, update, err) {
  mkdirpDurable(qdir) // durable dir-entry creation up the chain (fsync each new parent)
  const finalPath = join(qdir, `${Date.now()}-${update.update_id}.json`)
  const tmp = `${finalPath}.${randomUUID()}.tmp`
  const payload = JSON.stringify({ error: String(err?.message ?? err), update }, null, 2)
  let fd
  try {
    fd = openSync(tmp, 'wx', 0o600)
    writeFileSync(fd, payload)
    fsyncSync(fd)
    closeSync(fd); fd = undefined
    renameSync(tmp, finalPath)
    const d = openSync(qdir, 'r')
    try { fsyncSync(d) } finally { closeSync(d) }
  } finally {
    if (fd !== undefined) closeSync(fd)
    try { unlinkSync(tmp) } catch (e) { if (e.code !== 'ENOENT') throw e }
  }
}

async function main() {
  const stateDir = process.env.TG_BRIDGE_STATE_DIR || join(homedir(), '.cc-channels', '.telegram-bridge')
  const ccRoot = process.env.CC_CHANNELS_ROOT || join(homedir(), '.cc-channels')
  const longPollSeconds = Number(process.env.TG_BRIDGE_LONGPOLL || 25)
  mkdirpDurable(stateDir)
  const log = msg => console.error(`[telegram-bridge] ${new Date().toISOString()} ${msg}`)

  const releaseLock = await acquireLock(lockName(stateDir))

  // Fail-fast: bad config, ineligible label, missing token, or duplicate bot
  // identity aborts here (before any poller starts).
  const configured = loadBots(join(stateDir, 'bots.json'), ccRoot)

  const bots = configured.map(bot => {
    // Ledger is keyed by the STABLE bot id, not the label. Rotating the SECRET
    // of the SAME bot keeps the bot id, so its ledger (and update_id space) is
    // correctly RETAINED; only replacing the bot itself (a new bot id) yields a
    // fresh ledger. Keying by label would break both cases. (Fran finding.)
    const ledger = new Ledger(join(stateDir, `ledger-bot-${bot.botId}.json`))
    const qdir = join(stateDir, 'quarantine', bot.label)
    const quarantine = (update, err) => durableQuarantine(qdir, update, err)
    return { label: bot.label, token: bot.token, allowedChats: bot.allowedChats, ledger, quarantine }
  })

  log(`start: ${bots.length} bot(s) [${bots.map(b => b.label).join(', ')}], longpoll=${longPollSeconds}s`)

  const { stop, done } = runDaemon({
    bots,
    longPollSeconds,
    fetchFn: fetch,
    inject: m => injectToRelay(m, ccRoot),
    log,
  })

  for (const sig of ['SIGINT', 'SIGTERM']) process.once(sig, () => { log(`${sig} — stopping`); stop() })
  done.then(() => { log('stopped'); releaseLock(); process.exit(0) })
     .catch(err => { log(`fatal: ${err.message}`); releaseLock(); process.exit(1) })
}

main().catch(err => { console.error(`[telegram-bridge] fatal: ${err.message}`); process.exit(1) })
