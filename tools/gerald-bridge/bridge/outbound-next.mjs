#!/usr/bin/env node
// outbound-next.mjs — the deterministic half of Gerald's OUTBOUND bus-hop (ADR-0102).
//
// Gerald's send_message MCP tool spools one JSON file per outbound message into
// ~/gerald/outbox/<id>.json = {id, ts, from:"gerald", to, text}. The gerald-bridge
// Claude Code session (the bus-hop) runs THIS script to decide what to send,
// then calls SendMessage(to, text) itself, then acks. All routing/dedup logic
// lives here (deterministic); the LLM session only does the SendMessage.
//
//   node outbound-next.mjs peek        -> prints the next PENDING message as
//                                         JSON {id,to,text} (oldest first), or
//                                         nothing (exit 0) if none.
//   node outbound-next.mjs ack <id>    -> commit: move <id> to outbox/sent/.
//                                         The ack is idempotent (a second ack of
//                                         the same id is a no-op).
//
// Commit-after-delivery: the bus-hop peeks, SendMessages, then acks. A crash
// between send and ack re-peeks the same file, so it retries pending files until
// ack and NEVER LOSES a message — but DUPLICATE DELIVERY IS POSSIBLE (at-least-once
// delivery, the ADR-0102 outbound guarantee). The sent/ archive only stops
// re-peeking an already-acked file; it does not make delivery exactly-once.
// Identical-resend dedup (operation key) is a follow-up that needs send_message
// to stamp a stable op id.

import { readdir, rename, mkdir, readFile, stat, unlink, link } from 'node:fs/promises';
import { realpathSync } from 'node:fs';
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

const tmpRe = /^[0-9a-f-]{8,}\.json\.tmp$/i;
// A .tmp younger than this may be an in-flight write; older means the writer died.
const STALE_MS = 30_000;

// Recover orphan spool temp files. msg-server writes <id>.json.tmp, fsyncs, then
// renames to <id>.json (open -> sync -> rename). A crash in that window — e.g. a
// daemon restart — leaves a fully-fsynced <id>.json.tmp that pending() cannot see
// (idRe requires .json$), so the message is durable but undeliverable forever with
// no trace. ADR-0102 outbound must not silently lose a spooled message, so sweep on
// each peek. Age-gate so a concurrent live write is never touched; validate the JSON
// so a partial write (crash mid-writeFile, never acked to the caller) is discarded,
// not delivered; promote a complete orphan with link()+unlink() so an existing
// <id>.json (already spooled) is never clobbered.
export async function reapOrphans(outbox = OUTBOX, now = Date.now(), staleMs = STALE_MS) {
  const counts = { promoted: 0, deduped: 0, quarantined: 0 };
  let names;
  try { names = await readdir(outbox); } catch { return counts; }
  const sent = join(outbox, 'sent');
  const quarantine = join(outbox, 'quarantine');
  // Never silently discard bytes: a truncated or malformed tmp is a SIGNAL (disk
  // full, an fs bug, a SIGKILL mid-writeFile), and this is the only code that ever
  // sees it. Quarantine it for the operator and log the reason (mirrors poller.mjs;
  // the lesson is #403 B1 — a silent drop is the defect this bridge exists to avoid).
  const quarantineTmp = async (n, tmp, reason) => {
    try {
      await mkdir(quarantine, { recursive: true, mode: 0o700 });
      await rename(tmp, join(quarantine, n));
      counts.quarantined++;
      console.error(`reapOrphans: quarantined ${n} (${reason})`);
    } catch { /* leave it; a later sweep retries */ }
  };
  for (const n of names) {
    if (!tmpRe.test(n)) continue;
    const tmp = join(outbox, n);
    let info;
    try { info = await stat(tmp); } catch { continue; }
    if (now - info.mtimeMs < staleMs) continue; // maybe an in-flight write; leave it
    const id = n.replace(/\.json\.tmp$/i, '');
    let msg;
    try { msg = JSON.parse(await readFile(tmp, 'utf8')); }
    catch { await quarantineTmp(n, tmp, 'invalid JSON'); continue; }
    if (!msg || typeof msg.to !== 'string' || typeof msg.text !== 'string') {
      await quarantineTmp(n, tmp, 'bad shape'); continue;
    }
    // Already delivered (acked to sent/): the orphan is a stale duplicate. Drop it
    // WITHOUT re-promoting — otherwise a crash in the link->unlink window below
    // re-spools a sent message once the dest ages out of the pending set (NIT 1).
    try {
      await stat(join(sent, `${id}.json`));
      await unlink(tmp).catch(() => {});
      counts.deduped++;
      console.error(`reapOrphans: dropped ${n} (already in sent/)`);
      continue;
    } catch { /* not in sent/ — promote it */ }
    const dest = join(outbox, `${id}.json`);
    try {
      await link(tmp, dest); await unlink(tmp);               // atomic no-clobber promote
      counts.promoted++;
      console.error(`reapOrphans: promoted ${n} -> ${id}.json (was undeliverable)`);
    } catch (error) {
      if (error.code === 'EEXIST') {                          // already spooled and pending
        await unlink(tmp).catch(() => {});
        counts.deduped++;
        console.error(`reapOrphans: dropped ${n} (dest already spooled)`);
      }
    }
  }
  return counts;
}

async function peekNext() {
  await reapOrphans();
  const list = await pending();
  const peers = await validPeers();
  return list.find((m) => !peers || peers.has(m.to)) || null;
}

async function runCli() {
  const cmd = process.argv[2];
  if (cmd === 'peek') {
    const next = await peekNext();
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
}

// Compare realpaths: import.meta.url is already the realpath, but process.argv[1]
// is the path as invoked, and resolve() does NOT follow symlinks — so a symlinked
// invocation (the ADR-0104 canonical-checkout follow-up) would leave this false and
// silently no-op peek/ack, re-introducing the very silent loss this file now guards.
if (process.argv[1] && realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url))) runCli();
