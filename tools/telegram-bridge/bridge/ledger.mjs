// ledger.mjs — durable inbound state (ADR-0106). Delivery is AT-LEAST-ONCE (see
// ADR-0106 / README); this ledger reduces duplicates and gives correct resume,
// it does NOT make delivery exactly-once.
//
// Two facts persist across restarts:
//   * offset  — the Telegram getUpdates ack. Passing offset=N confirms every
//     update < N and returns updates >= N, so advancing offset to updateId+1
//     AFTER each successful inject means a crash mid-batch re-fetches only the
//     un-injected tail. Offset alone gives correct resume.
//   * seen    — a bounded set of recently-injected update_ids. Best-effort
//     dedup: a re-fetched update that was already injected is skipped, cutting
//     the common redelivery case. It does not eliminate duplicates (the
//     inject-then-ack window and the channel server's own deliver-then-unlink
//     both remain at-least-once).
//
// Writes are atomic (tmp + fsync + rename) so a crash never leaves a torn
// ledger. The seen set is capped so the file cannot grow without bound.

import { closeSync, fsyncSync, mkdirSync, openSync, readFileSync, renameSync, unlinkSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { randomUUID } from 'node:crypto'

const SEEN_CAP = 1000

function atomicWriteJson(path, value) {
  const tmp = `${path}.${randomUUID()}.tmp`
  let fd
  try {
    fd = openSync(tmp, 'wx', 0o600)
    writeFileSync(fd, JSON.stringify(value) + '\n')
    fsyncSync(fd)
    closeSync(fd)
    fd = undefined
    renameSync(tmp, path)
    const d = openSync(dirname(path), 'r')
    try { fsyncSync(d) } finally { closeSync(d) }
  } finally {
    if (fd !== undefined) closeSync(fd)
    try { unlinkSync(tmp) } catch (e) { if (e.code !== 'ENOENT') throw e }
  }
}

export class Ledger {
  constructor(path) {
    this.path = resolve(path)
    // The containing dir (the daemon's state dir) is created DURABLY by
    // bridge.mjs (mkdirpDurable) before any Ledger is constructed, so this
    // recursive mkdir is only a standalone/test convenience. Each write is
    // atomic + fsync (payload and parent dir), so the ack-of-record is durable
    // given that pre-existing dir. (Gerald N1 — deliberate, ordering-owned.)
    mkdirSync(dirname(this.path), { recursive: true })
    this.offset = 0
    this.seen = []
    this._seenSet = new Set()
    this._load()
  }

  _load() {
    let raw
    try { raw = JSON.parse(readFileSync(this.path, 'utf8')) }
    catch (e) { if (e.code === 'ENOENT') return; throw e }
    if (!Number.isInteger(raw.offset) || raw.offset < 0) throw new Error('ledger: bad offset')
    if (!Array.isArray(raw.seen) || !raw.seen.every(Number.isInteger)) throw new Error('ledger: bad seen')
    this.offset = raw.offset
    this.seen = raw.seen.slice(-SEEN_CAP)
    this._seenSet = new Set(this.seen)
  }

  has(updateId) { return this._seenSet.has(updateId) }

  // Persist-then-mutate: compute the NEXT state, write it durably, and only then
  // publish it into memory. If the write throws, in-memory offset/seen are
  // unchanged, so the next getUpdates re-fetches from the un-advanced offset
  // rather than acking an advance that never hit disk (Bert/Fran finding).
  _apply(nextOffset, nextSeen) {
    atomicWriteJson(this.path, { offset: nextOffset, seen: nextSeen })
    this.offset = nextOffset
    this.seen = nextSeen
    this._seenSet = new Set(nextSeen)
  }

  // A processed-but-not-injected update (disallowed chat, non-text). Advance the
  // ack past it so it is not re-fetched forever, but do NOT mark it seen.
  advance(updateId) {
    if (!Number.isInteger(updateId)) throw new Error('updateId must be an integer')
    const nextOffset = Math.max(this.offset, updateId + 1)
    if (nextOffset === this.offset) return // already past it; no write needed
    this._apply(nextOffset, this.seen)
  }

  // A successfully injected update: mark it seen (best-effort dedup to reduce
  // redelivery — the contract is at-least-once, see README/ADR-0106) and advance
  // the ack to updateId+1. offset only moves forward.
  commit(updateId) {
    if (!Number.isInteger(updateId)) throw new Error('updateId must be an integer')
    const nextOffset = Math.max(this.offset, updateId + 1)
    let nextSeen = this.seen
    if (!this._seenSet.has(updateId)) {
      nextSeen = this.seen.concat(updateId)
      if (nextSeen.length > SEEN_CAP) nextSeen = nextSeen.slice(-SEEN_CAP)
    }
    this._apply(nextOffset, nextSeen)
  }
}
