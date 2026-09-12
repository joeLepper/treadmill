// ledger.mjs — durable inbound state (ADR-0106 / ADR-0093 effectively-once).
//
// Two facts persist across restarts:
//   * offset  — the Telegram getUpdates ack. Passing offset=N confirms every
//     update < N and returns updates >= N, so advancing offset to updateId+1
//     AFTER each successful inject means a crash mid-batch re-fetches only the
//     un-injected tail. Offset alone gives correct resume.
//   * seen    — a bounded set of recently-injected update_ids. Belt-and-
//     suspenders effectively-once: even if the offset logic regressed, a
//     re-fetched update that was already injected is skipped. This is the
//     computable guard behind the ADR-0106 falsifier ("delivered twice").
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

  _advanceOffset(updateId) {
    if (!Number.isInteger(updateId)) throw new Error('updateId must be an integer')
    if (updateId + 1 > this.offset) this.offset = updateId + 1
  }

  _persist() { atomicWriteJson(this.path, { offset: this.offset, seen: this.seen }) }

  // A processed-but-not-injected update (unmapped chat, non-text). Advance the
  // ack past it so it is not re-fetched forever, but do NOT mark it seen.
  advance(updateId) {
    this._advanceOffset(updateId)
    this._persist()
  }

  // A successfully injected update: mark it seen (effectively-once) and advance
  // the ack to updateId+1. Persist atomically. offset only moves forward.
  commit(updateId) {
    this._advanceOffset(updateId)
    if (!this._seenSet.has(updateId)) {
      this._seenSet.add(updateId)
      this.seen.push(updateId)
      if (this.seen.length > SEEN_CAP) {
        const dropped = this.seen.splice(0, this.seen.length - SEEN_CAP)
        for (const d of dropped) this._seenSet.delete(d)
      }
    }
    this._persist()
  }
}
