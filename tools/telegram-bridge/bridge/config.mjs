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

// Read TELEGRAM_BOT_TOKEN from a session's telegram.env. Minimal KEY=VALUE parse;
// no shell evaluation. A quoted value is used verbatim; an unquoted value is
// taken up to the first whitespace, so a trailing inline comment or trailing
// spaces are not captured into the token (Bert finding).
export function loadToken(label, ccRoot = join(homedir(), '.cc-channels')) {
  assertLabel(label)
  const path = join(ccRoot, label, 'telegram.env')
  const text = readFileSync(path, 'utf8')
  for (const line of text.split('\n')) {
    const m = line.match(/^\s*(?:export\s+)?TELEGRAM_BOT_TOKEN\s*=\s*(.*)$/)
    if (!m) continue
    let v = m[1].trim()
    if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) v = v.slice(1, -1)
    else v = v.split(/\s/)[0] // unquoted: token has no whitespace; drop inline comment/trailing ws
    if (v) return v
  }
  throw new Error(`no TELEGRAM_BOT_TOKEN in ${path}`)
}

// The public bot id — the integer prefix of a "<botId>:<secret>" token. Stable
// per bot; safe to log and to key state by (it is NOT the secret).
export function botIdOf(token) {
  const id = String(token).split(':', 1)[0]
  if (!/^\d+$/.test(id)) throw new Error('token is not in <botId>:<secret> form')
  return id
}

// Load the full bot config: parse, gate each label on watcher-eligibility, load
// each token, and REJECT duplicate bot identity. Returns
// [{ label, allowedChats:Set, token, botId }]. Fail-fast; errors never print a token.
export function loadBots(configPath, ccRoot = join(homedir(), '.cc-channels')) {
  const bots = parseConfig(JSON.parse(readFileSync(configPath, 'utf8')))
  const byBotId = new Map()
  const loaded = bots.map(bot => {
    if (!isWatcherEligible(bot.label, ccRoot)) {
      throw new Error(`bot ${bot.label} is not a launcher-managed session (no session-id record) — refusing to bridge a label whose relay dir no watcher consumes`)
    }
    const token = loadToken(bot.label, ccRoot)
    const botId = botIdOf(token)
    // Two labels on the SAME bot would put two pollers on one token (recreates
    // 409 contention) and cross-deliver. Reject; name the labels + botId, never
    // the token.
    if (byBotId.has(botId)) {
      throw new Error(`bots ${byBotId.get(botId)} and ${bot.label} share bot id ${botId} (same token) — refusing to run two pollers on one bot`)
    }
    byBotId.set(botId, bot.label)
    return { ...bot, token, botId }
  })
  return loaded
}
