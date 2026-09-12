#!/usr/bin/env node
// outbound-next.mjs — the deterministic half of Fran's OUTBOUND bus-hop (ADR-0102).
//
// Fran's send_message MCP tool spools one JSON file per outbound message into
// ~/fran/outbox/<id>.json = {id, ts, from:"fran", to, text}. The fran-bridge
// Claude Code session (the bus-hop) runs THIS script to decide what to send,
// then calls SendMessage(to, text) itself, then acks. All routing/dedup logic
// lives here (deterministic); the LLM session only does the SendMessage.
//
//   node outbound-next.mjs peek        -> prints the next PENDING message as
//                                         JSON {id,to,text} (oldest first), or
//                                         nothing (exit 0) if none.
//   node outbound-next.mjs ack <id>    -> commit: move <id> to outbox/sent/
//                                         (delivered exactly once). Idempotent.
//
// Commit-after-delivery: the bus-hop peeks, SendMessages, then acks — so a crash
// between send and ack re-peeks the same message (at-least-once delivery, the
// ADR-0102 outbound guarantee), never loses it. Each file is delivered once via
// the sent/ archive; identical-resend dedup (operation key) is a follow-up that
// needs send_message to stamp a stable op id.

import { readdir, rename, mkdir, readFile } from 'node:fs/promises';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const OUTBOX = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'outbox');
const SENT = join(OUTBOX, 'sent');
const PEERS_FILE = resolve(OUTBOX, '..', 'peers.json');

const idRe = /^[0-9a-f-]{8,}\.json$/i;

async function pending() {
  let names;
  try { names = await readdir(OUTBOX); } catch { return []; }
  const files = names.filter((n) => idRe.test(n)).sort(); // uuid v4 ordering is not chronological; fall back to mtime below
  const withStat = [];
  for (const n of files) {
    try {
      const raw = await readFile(join(OUTBOX, n), 'utf8');
      const msg = JSON.parse(raw);
      if (msg && typeof msg.to === 'string' && typeof msg.text === 'string') {
        withStat.push({ id: n.replace(/\.json$/i, ''), to: msg.to, text: msg.text, ts: msg.ts || '' });
      }
    } catch { /* skip malformed / mid-write */ }
  }
  withStat.sort((a, b) => String(a.ts).localeCompare(String(b.ts)));
  return withStat;
}

async function validPeers() {
  try { return new Set(JSON.parse(await readFile(PEERS_FILE, 'utf8'))); } catch { return null; }
}

const cmd = process.argv[2];

if (cmd === 'peek') {
  const list = await pending();
  const peers = await validPeers();
  const next = list.find((m) => !peers || peers.has(m.to));
  if (next) process.stdout.write(JSON.stringify({ id: next.id, to: next.to, text: next.text }) + '\n');
  process.exit(0);
} else if (cmd === 'ack') {
  const id = process.argv[3];
  if (!id || !/^[0-9a-f-]{8,}$/i.test(id)) { console.error('ack requires a message id'); process.exit(2); }
  await mkdir(SENT, { recursive: true, mode: 0o700 });
  try { await rename(join(OUTBOX, `${id}.json`), join(SENT, `${id}.json`)); } catch { /* already acked = idempotent */ }
  process.exit(0);
} else {
  console.error('usage: outbound-next.mjs peek | ack <id>');
  process.exit(2);
}
