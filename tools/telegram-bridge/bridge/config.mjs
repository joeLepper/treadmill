// config.mjs — the daemon's bot list (ADR-0106, multi-token). Each entry names a
// session label and the chat id(s) that bot is allowed to receive from. The
// token is NOT in this config: it is read from the session's existing
// ~/.cc-channels/<label>/telegram.env (same UID, mode 0600), so the daemon
// concentrates the tokens only at runtime and adds no new secret file.
//
// Config shape (~/.cc-channels/.telegram-bridge/bots.json):
//   { "bots": [ { "label": "treadmill-carla", "allowedChats": [8956818786] }, ... ] }
//
// Startup is fail-fast: an unknown/pure-fabric label (no launcher session-id
// record) or a bot with no token in telegram.env aborts the daemon rather than
// acking messages it can never deliver.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { homedir } from 'node:os'
import { assertLabel, isWatcherEligible } from './inject.mjs'

// Parse + validate the config object into a list of { label, allowedChats:Set }.
export function parseConfig(obj) {
  if (obj === null || typeof obj !== 'object' || Array.isArray(obj)) {
    throw new Error('config must be an object')
  }
  if (!Array.isArray(obj.bots) || obj.bots.length === 0) {
    throw new Error('config must have a non-empty "bots" array')
  }
  const seen = new Set()
  return obj.bots.map(bot => {
    if (bot === null || typeof bot !== 'object') throw new Error('each bot must be an object')
    assertLabel(bot.label)
    if (seen.has(bot.label)) throw new Error(`duplicate bot label: ${bot.label}`)
    seen.add(bot.label)
    if (!Array.isArray(bot.allowedChats) || bot.allowedChats.length === 0) {
      throw new Error(`bot ${bot.label} must have a non-empty "allowedChats" array (the mandatory sender allowlist)`)
    }
    const allowedChats = new Set()
    for (const c of bot.allowedChats) {
      if (!Number.isInteger(c)) throw new Error(`bot ${bot.label} allowedChats must be integers: ${JSON.stringify(c)}`)
      allowedChats.add(String(c))
    }
    return { label: bot.label, allowedChats }
  })
}

// Read TELEGRAM_BOT_TOKEN from a session's telegram.env. Minimal KEY=VALUE parse
// (strips optional surrounding quotes); no shell evaluation.
export function loadToken(label, ccRoot = join(homedir(), '.cc-channels')) {
  assertLabel(label)
  const path = join(ccRoot, label, 'telegram.env')
  const text = readFileSync(path, 'utf8')
  for (const line of text.split('\n')) {
    const m = line.match(/^\s*(?:export\s+)?TELEGRAM_BOT_TOKEN\s*=\s*(.*)\s*$/)
    if (m) {
      let v = m[1].trim()
      if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) v = v.slice(1, -1)
      if (v) return v
    }
  }
  throw new Error(`no TELEGRAM_BOT_TOKEN in ${path}`)
}

// Load the full bot config: parse, gate each label on watcher-eligibility, load
// each token. Returns [{ label, allowedChats:Set, token }]. Fail-fast.
export function loadBots(configPath, ccRoot = join(homedir(), '.cc-channels')) {
  const bots = parseConfig(JSON.parse(readFileSync(configPath, 'utf8')))
  return bots.map(bot => {
    if (!isWatcherEligible(bot.label, ccRoot)) {
      throw new Error(`bot ${bot.label} is not a launcher-managed session (no session-id record) — refusing to bridge a label whose relay dir no watcher consumes`)
    }
    return { ...bot, token: loadToken(bot.label, ccRoot) }
  })
}
