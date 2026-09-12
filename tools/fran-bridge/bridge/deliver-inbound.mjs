#!/usr/bin/env node
// deliver-inbound.mjs — the deterministic inbound wrapper the fran-bridge session
// calls per inbound bus message. It derives a STABLE dedupKey from (from,text) so
// a redelivered identical message dedups, then hands off to inbound.mjs deliver,
// which owns the ledger + codex queue + commit-on-completion (ADR-0102).
//
//   node deliver-inbound.mjs <from> <text...>
//
// Prints inbound.mjs's JSON result on stdout. A "submitted"/"exists" result means
// the turn is queued to Fran; her reply flows back out through the outbound relay.
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const from = process.argv[2] || '';
const text = process.argv.slice(3).join(' ');
if (!from || !text) { console.error('usage: deliver-inbound.mjs <from> <text...>'); process.exit(2); }

// Stable across identical redeliveries of the same bus message (harness SendMessage
// is one-shot, so the main redelivery risk is a bridge crash re-draining its inbox).
const dedupKey = 'bus-' + createHash('sha256').update(from + '\n' + text).digest('hex').slice(0, 40);
const inbound = join(dirname(fileURLToPath(import.meta.url)), 'inbound.mjs');
const r = spawnSync('node', [inbound, 'deliver'], { input: JSON.stringify({ dedupKey, from, text }), encoding: 'utf8' });
if (r.stdout) process.stdout.write(r.stdout);
if (r.stderr) process.stderr.write(r.stderr);
process.exit(r.status ?? 1);
