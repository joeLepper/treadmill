// durable.mjs — filesystem durability helpers (ADR-0106).
//
// mkdirpDurable(dir, anchor): make `dir` exist AND make every directory entry
// from `anchor` (exclusive) down to `dir` (inclusive) DURABLE. Per fsync(2), a
// directory entry is durable only once its CONTAINING directory is fsync-ed.
//
// The subtle bug this closes (Fran, executed foil): treating exists() as proof
// of durability. If a prior call created a dir but crashed/threw before the
// parent fsync, a retry that sees the dir "exists" and skips the fsync leaves a
// non-durable entry — and this is exactly runBot's retry path after a
// quarantine throw, so a later quarantine could advance the durable ledger over
// a dir-entry that is never synced (an acked operator message lost on the next
// crash). So we fsync the parent of EVERY component on EVERY call, whether we
// created it or found it, and verify each is a directory.
//
// `anchor` must be an existing directory assumed already-durable (it predates
// the daemon — e.g. the cc-channels root or $HOME). It bounds the fsync work to
// the path below it. Ops are injectable for a mutation-sensitive test (a test
// can assert the parent fsync happens even when the dir already exists).

import { closeSync, fsyncSync, mkdirSync, openSync, statSync } from 'node:fs'
import { dirname, resolve } from 'node:path'

const realFsyncDir = dir => {
  const fd = openSync(dir, 'r')
  try { fsyncSync(fd) } finally { closeSync(fd) }
}
const realStatDir = p => {
  try { return statSync(p).isDirectory() ? 'dir' : 'notdir' } catch (e) { if (e.code === 'ENOENT') return 'missing'; throw e }
}

export function mkdirpDurable(dir, anchor, ops = {}) {
  const mkdir = ops.mkdir ?? (d => mkdirSync(d))
  const fsyncDir = ops.fsyncDir ?? realFsyncDir
  const statDir = ops.statDir ?? realStatDir

  dir = resolve(dir)
  anchor = resolve(anchor)
  if (statDir(anchor) !== 'dir') throw new Error(`anchor is not an existing directory: ${anchor}`)

  // Components strictly below the anchor, shallow→deep.
  const comps = []
  let p = dir
  while (p !== anchor) {
    comps.push(p)
    const parent = dirname(p)
    if (parent === p) throw new Error(`anchor ${anchor} is not an ancestor of ${dir}`)
    p = parent
  }
  comps.reverse()

  for (const d of comps) {
    const kind = statDir(d)
    if (kind === 'missing') mkdir(d)
    else if (kind !== 'dir') throw new Error(`path component exists but is not a directory: ${d}`)
    // Fsync the PARENT whether or not we created d — a pre-existing entry may be
    // from a prior run that never synced (do not treat exists() as durable).
    fsyncDir(dirname(d))
  }
  return dir
}
