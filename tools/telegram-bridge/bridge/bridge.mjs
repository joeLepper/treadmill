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

import { closeSync, mkdirSync, openSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { homedir } from 'node:os'
import { injectToRelay } from './inject.mjs'
import { loadBots } from './config.mjs'
import { Ledger } from './ledger.mjs'
import { runDaemon } from './poller.mjs'

// Single-instance pid-lock (Bert finding): "sole poller" must be enforced, not
// just implied by the systemd unit name — a stray `node bridge.mjs` would
// double-poll every token (409 on all). We hold an O_EXCL pidfile and treat a
// live pid as a held lock (launcher's kill -0 pattern); a stale file is cleared.
function acquireLock(pidfile) {
  try {
    const fd = openSync(pidfile, 'wx')
    writeFileSync(fd, String(process.pid)); closeSync(fd)
  } catch (e) {
    if (e.code !== 'EEXIST') throw e
    const held = Number(readFileSync(pidfile, 'utf8').trim())
    if (Number.isInteger(held) && held > 0) {
      try { process.kill(held, 0); throw new Error(`another telegram-bridge is alive (pid ${held})`) }
      catch (ke) { if (ke.code !== 'ESRCH') throw ke } // ESRCH = stale
    }
    unlinkSync(pidfile)
    const fd = openSync(pidfile, 'wx')
    writeFileSync(fd, String(process.pid)); closeSync(fd)
  }
  return () => { try { unlinkSync(pidfile) } catch { /* best effort */ } }
}

function main() {
  const stateDir = process.env.TG_BRIDGE_STATE_DIR || join(homedir(), '.cc-channels', '.telegram-bridge')
  const ccRoot = process.env.CC_CHANNELS_ROOT || join(homedir(), '.cc-channels')
  const longPollSeconds = Number(process.env.TG_BRIDGE_LONGPOLL || 25)
  mkdirSync(stateDir, { recursive: true })
  const log = msg => console.error(`[telegram-bridge] ${new Date().toISOString()} ${msg}`)

  const releaseLock = acquireLock(join(stateDir, 'bridge.pid'))

  // Fail-fast: bad config, ineligible label, or missing token aborts here.
  const configured = loadBots(join(stateDir, 'bots.json'), ccRoot)

  const bots = configured.map(bot => {
    const ledger = new Ledger(join(stateDir, `ledger-${bot.label}.json`))
    const qdir = join(stateDir, 'quarantine', bot.label)
    const quarantine = (update, err) => {
      mkdirSync(qdir, { recursive: true })
      const p = join(qdir, `${Date.now()}-${update.update_id}.json`)
      writeFileSync(p, JSON.stringify({ error: String(err?.message ?? err), update }, null, 2))
    }
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

main()
