// durable.mjs — filesystem durability helpers (ADR-0106).
//
// mkdirpDurable: create a directory chain such that every NEW directory entry
// is durable. Per fsync(2), fsync-ing a file does not make its directory ENTRY
// durable — the containing directory must be fsync-ed. A lazily-created
// quarantine/relay subtree that is not synced up the chain can lose
// reachability after a machine crash even though a fsynced ledger offset
// survived — i.e. an acked update with no on-disk copy (Fran finding). So after
// creating each missing component we fsync its PARENT.

import { closeSync, fsyncSync, mkdirSync, openSync, statSync } from 'node:fs'
import { dirname, resolve } from 'node:path'

function exists(p) {
  try { statSync(p); return true } catch (e) { if (e.code === 'ENOENT') return false; throw e }
}

function fsyncDir(dir) {
  const fd = openSync(dir, 'r')
  try { fsyncSync(fd) } finally { closeSync(fd) }
}

export function mkdirpDurable(dir) {
  dir = resolve(dir)
  const missing = []
  let p = dir
  while (!exists(p)) {
    missing.push(p)
    const parent = dirname(p)
    if (parent === p) break // reached the root
    p = parent
  }
  // `missing` is deepest→shallowest; create shallowest→deepest so each parent
  // exists before its child, and fsync each new dir's parent for entry durability.
  for (const d of missing.reverse()) {
    mkdirSync(d)
    fsyncDir(dirname(d))
  }
  return dir
}
