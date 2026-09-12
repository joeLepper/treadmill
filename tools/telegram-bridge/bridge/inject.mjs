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

import { closeSync, fsyncSync, lstatSync, openSync, realpathSync, renameSync, unlinkSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { homedir } from 'node:os'
import { randomUUID } from 'node:crypto'
import { mkdirpDurable } from './durable.mjs'

// A session label is used as a filesystem path segment. Reject anything that is
// not a plain label so a crafted config can never escape the relay root.
const LABEL_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/

export function assertLabel(label) {
  if (typeof label !== 'string' || !LABEL_RE.test(label)) {
    throw new Error(`invalid session label: ${JSON.stringify(label)}`)
  }
  return label
}

// Reject a symlink at path (if it exists). A symlinked <label> or <label>/relay
// could redirect a message into ANOTHER session's dir (Fran finding). We lstat
// (does not follow) and refuse a symlink component. This is a same-UID system,
// so this guards against accidental/symlink misconfiguration, NOT a hostile
// same-UID process — a concurrent rewrite between check and write (TOCTOU)
// remains possible and is disclosed, not defended, at this trust boundary.
function assertNotSymlink(path) {
  let st
  try { st = lstatSync(path) } catch (e) { if (e.code === 'ENOENT') return; throw e }
  if (st.isSymbolicLink()) throw new Error(`relay path component is a symlink (refused): ${path}`)
  return st
}

// The lexical target dir. Containment is enforced by realpath checks in
// injectToRelay, not by this string alone.
export function relayDir(label, ccRoot = join(homedir(), '.cc-channels')) {
  assertLabel(label)
  const root = resolve(ccRoot)
  const dir = join(root, label, 'relay')
  if (dir !== resolve(root, label, 'relay')) throw new Error(`relay dir escapes root: ${dir}`)
  return dir
}

// Resolve the relay dir with symlink rejection on the <label> and relay
// components, returning the lexical path. Existing components are lstat-checked;
// missing ones are created by the caller and re-verified by realpath afterward.
function safeRelayDir(label, ccRoot) {
  assertLabel(label)
  const root = resolve(ccRoot)
  const labelDir = join(root, label)
  const dir = join(labelDir, 'relay')
  assertNotSymlink(labelDir)
  assertNotSymlink(dir)
  return { root, labelDir, dir }
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

// Eligibility (Fran finding): a target is deliverable only if it is a
// launcher-managed session — the launcher writes a `session-id` file into
// ~/.cc-channels/<label>/, and launch-session.sh runs the treadmill-events
// watcher for every launcher session. A pure-fabric or unknown label has no
// such record, so injecting there would ack to Telegram but never be consumed
// (silent loss). Checked at startup so a bad config fails fast, not per-message.
export function isWatcherEligible(label, ccRoot = join(homedir(), '.cc-channels')) {
  assertLabel(label)
  try { return lstatSync(join(resolve(ccRoot), label, 'session-id')).isFile() }
  catch (e) { if (e.code === 'ENOENT') return false; throw e }
}

// Write one message into the target session's relay inbox. Returns the path.
// mkdir is recursive+idempotent so a not-yet-started (but eligible) target still
// receives the file (the channel server drains the dir on its next start — the
// durable leg). Delivery is AT-LEAST-ONCE: this publishes, the caller acks, and
// the channel server delivers-then-unlinks; a crash in either window redelivers.
export function injectToRelay(
  { label, text, from, chatId, updateId, date },
  ccRoot = join(homedir(), '.cc-channels'),
) {
  const { root, labelDir, dir } = safeRelayDir(label, ccRoot)
  // Anchor at ccRoot (predates the daemon, assumed durable); this makes the
  // <label> and relay entries durable on every call, not just when created.
  mkdirpDurable(dir, root)
  // Post-mkdir realpath check: the created (or pre-existing) relay dir must
  // still resolve under <root>/<label>, i.e. no symlink slipped in. Rejects the
  // configured-label -> wrong-session-dir foil.
  if (realpathSync(dir) !== join(realpathSync(labelDir), 'relay')) {
    throw new Error(`relay dir does not resolve under its label dir: ${dir}`)
  }
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

export const _internal = { LABEL_RE }
