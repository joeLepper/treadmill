#!/usr/bin/env node
// bridge.mjs — the Telegram bridge daemon entrypoint (ADR-0106). Wires config
// into the sole poller: token from env (never a file), routing table + ledger
// from the state dir. Run under systemd-user (see systemd/telegram-bridge.service).
//
//   TELEGRAM_BOT_TOKEN=... node bridge.mjs
//
// Env:
//   TELEGRAM_BOT_TOKEN   (required) the shared bot token — the one contended slot
//   TG_BRIDGE_STATE_DIR  state dir (default ~/.cc-channels/.telegram-bridge)
//   TG_BRIDGE_LONGPOLL   long-poll seconds (default 25)
//   CC_CHANNELS_ROOT     relay root (default ~/.cc-channels)

import { join } from 'node:path'
import { homedir } from 'node:os'
import { injectToRelay } from './inject.mjs'
import { loadRouting, resolveLabel } from './routing.mjs'
import { Ledger } from './ledger.mjs'
import { runPoller } from './poller.mjs'

function main() {
  const token = process.env.TELEGRAM_BOT_TOKEN
  if (!token) { console.error('bridge: TELEGRAM_BOT_TOKEN is required'); process.exit(2) }

  const stateDir = process.env.TG_BRIDGE_STATE_DIR || join(homedir(), '.cc-channels', '.telegram-bridge')
  const ccRoot = process.env.CC_CHANNELS_ROOT || join(homedir(), '.cc-channels')
  const longPollSeconds = Number(process.env.TG_BRIDGE_LONGPOLL || 25)

  const table = loadRouting(join(stateDir, 'routing.json'))
  const ledger = new Ledger(join(stateDir, 'ledger.json'))
  const log = msg => console.error(`[telegram-bridge] ${new Date().toISOString()} ${msg}`)

  log(`start: ${table.size} route(s), offset=${ledger.offset}, longpoll=${longPollSeconds}s`)

  const { stop, done } = runPoller({
    token,
    longPollSeconds,
    fetchFn: fetch,
    inject: m => injectToRelay(m, ccRoot),
    resolveLabel: chatId => resolveLabel(table, chatId),
    ledger,
    log,
  })

  // An intentional stop (SIGTERM from systemd) ends the loop cleanly → exit 0
  // (SuccessExitStatus in the unit). We do NOT swallow a crash: if `done`
  // rejects, exit non-zero so systemd restarts (matches fran/gerald supervisors).
  for (const sig of ['SIGINT', 'SIGTERM']) process.once(sig, () => { log(`${sig} — stopping`); stop() })
  done.then(() => { log('stopped'); process.exit(0) })
     .catch(err => { log(`fatal: ${err.message}`); process.exit(1) })
}

main()
