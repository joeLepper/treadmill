// inject.mjs — the inbound seam (ADR-0106). Writes one Telegram message as a
// `.md` file into a target session's channel relay inbox
// (~/.cc-channels/<label>/relay/). That session's treadmill-events channel
// server watches the dir (relay-inbox.ts, task ecd6d6eb) and injects the file
// as a <channel source="relay"> notification, then unlinks it (at-least-once).
//
// WHY the base relay dir, not a subfolder: treadmill-events.ts watches the base
// dir plus a HARDCODED subfolder set (['coord','worker']). A 'telegram'
// subfolder would not be watched. So we write to the base dir; the content
// header below disambiguates a Telegram-operator message from a sibling relay.
//
// WHY atomic rename: the channel server's fs.watch fires on file creation and
// reads immediately. A direct write can be read half-formed. We write to a
// sibling `.tmp` in the SAME dir, fsync, then rename into place — the watcher
// only ever sees the complete `.md`. (The rename lands in the watched dir, so
// fs.watch still fires for it.)

import { closeSync, fsyncSync, mkdirSync, openSync, renameSync, unlinkSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { homedir } from 'node:os'
import { randomUUID } from 'node:crypto'

// A session label is used as a filesystem path segment. Reject anything that is
// not a plain label so a crafted chat mapping can never escape the relay root.
const LABEL_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/

export function assertLabel(label) {
  if (typeof label !== 'string' || !LABEL_RE.test(label)) {
    throw new Error(`invalid session label: ${JSON.stringify(label)}`)
  }
  return label
}

export function relayDir(label, ccRoot = join(homedir(), '.cc-channels')) {
  assertLabel(label)
  // Resolve and re-check containment: belt-and-suspenders against a ccRoot that
  // itself contains traversal, and a guard the reviewer can point a foil at.
  const root = resolve(ccRoot)
  const dir = resolve(root, label, 'relay')
  if (dir !== join(root, label, 'relay')) {
    throw new Error(`relay dir escapes root: ${dir}`)
  }
  return dir
}

// The content the receiving session sees. It arrives tagged source="relay", so
// the header must make it unambiguous that this is a Telegram message from the
// operator and that the reply goes back over Telegram (reply-on-same-surface).
export function formatBody({ text, from, chatId, updateId, date }) {
  const who = from ? ` from ${from}` : ''
  const when = date ? new Date(date * 1000).toISOString() : new Date().toISOString()
  return [
    `[telegram inbound via bridge — ADR-0106]${who}`,
    `chat_id=${chatId} update_id=${updateId} at=${when}`,
    'Reply on Telegram: direct Bot API sendMessage to this chat_id (outbound bypasses the poll slot).',
    '',
    text,
    '',
  ].join('\n')
}

// Write one message into the target session's relay inbox. Returns the path.
// mkdir is recursive+idempotent so a not-yet-started target still receives the
// file (the channel server drains the dir on its next start — durable leg).
export function injectToRelay(
  { label, text, from, chatId, updateId, date },
  ccRoot = join(homedir(), '.cc-channels'),
) {
  const dir = relayDir(label, ccRoot)
  mkdirSync(dir, { recursive: true })
  const body = formatBody({ text, from, chatId, updateId, date })
  const name = `tg-${Date.now()}-${updateId}-${randomUUID().slice(0, 8)}.md`
  const finalPath = join(dir, name)
  const tmp = `${finalPath}.${randomUUID()}.tmp`
  let fd
  try {
    fd = openSync(tmp, 'wx', 0o600)
    writeFileSync(fd, body)
    fsyncSync(fd)
    closeSync(fd)
    fd = undefined
    renameSync(tmp, finalPath)
    const d = openSync(dir, 'r')
    try { fsyncSync(d) } finally { closeSync(d) }
  } finally {
    if (fd !== undefined) closeSync(fd)
    try { unlinkSync(tmp) } catch (e) { if (e.code !== 'ENOENT') throw e }
  }
  return finalPath
}

export const _internal = { LABEL_RE, dirname }
