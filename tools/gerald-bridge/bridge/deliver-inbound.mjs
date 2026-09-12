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
const rawText = process.argv.slice(3).join(' ');
if (!from || !rawText) { console.error('usage: deliver-inbound.mjs <from> <text...>'); process.exit(2); }

// Gerald runs in an ISOLATED Codex home (ADR-0104). The relay's tmux session does
// not inherit the unit's CODEX_HOME (shared tmux server, ADR-0101), so pin it here
// — inbound.mjs's `codex queue` and rollout discovery must target Gerald's daemon.
process.env.CODEX_HOME ||= '/home/joe/gerald/.codex';

// Per-message model selection (ADR-0104): a leading [[model: <name>]] marker picks
// which OpenCode Go model handles this turn. The bridge sets it via `codex queue
// --model` (deterministic), so we strip the marker from the text Gerald sees.
// Only allowlisted models pass through; anything else falls back to the default.
const MODELS = new Set(['qwen3.8-max', 'kimi-k2.7-code', 'glm-5.2', 'glm-5.3', 'minimax-m3']);
let text = rawText, model;
const mk = rawText.match(/^\s*\[\[model:\s*([A-Za-z0-9._-]{1,64})\]\]\s*/);
if (mk) {
  text = rawText.slice(mk[0].length);
  if (MODELS.has(mk[1])) model = mk[1];       // valid -> per-turn override
  // invalid model name -> ignore marker, use default (text already stripped)
}

// dedupKey is derived from the ORIGINAL text (marker included), so the same request
// dedups and a different-model request is a distinct turn. Stable across a bridge
// crash re-draining its inbox (harness SendMessage is one-shot).
const dedupKey = 'bus-' + createHash('sha256').update(from + '\n' + rawText).digest('hex').slice(0, 40);
const payload = model ? { dedupKey, from, text, model } : { dedupKey, from, text };
const inbound = join(dirname(fileURLToPath(import.meta.url)), 'inbound.mjs');
const r = spawnSync('node', [inbound, 'deliver'], { input: JSON.stringify(payload), encoding: 'utf8' });
if (r.stdout) process.stdout.write(r.stdout);
if (r.stderr) process.stderr.write(r.stderr);
process.exit(r.status ?? 1);
