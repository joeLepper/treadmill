import { EventEmitter } from 'node:events';
import { createHash, randomUUID } from 'node:crypto';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { DatabaseSync } from 'node:sqlite';
import {
  closeSync, fsyncSync, mkdirSync, openSync, readFileSync,
  readdirSync, renameSync, unlinkSync, writeFileSync,
} from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { homedir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { createInterface } from 'node:readline';

const HOME = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const exec = promisify(execFile);
const PREFIX = 'FRAN_INBOUND_V1\n';
const states = new Set(['submitted', 'running', 'completed', 'confirmed-absent']);
const hash = value => createHash('sha256').update(value).digest('hex');
const nonempty = value => typeof value === 'string' && value.trim().length > 0;
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const now = () => new Date().toISOString();

function validateMessage(value) {
  if (!object(value) || Object.keys(value).some(k => !['dedupKey', 'from', 'text'].includes(k)) ||
      !nonempty(value.dedupKey) || !nonempty(value.from) || !nonempty(value.text) ||
      Buffer.byteLength(value.dedupKey) > 4096 || Buffer.byteLength(value.from) > 256 ||
      Buffer.byteLength(value.text) > 65536) {
    throw new Error('Expected {dedupKey, from, text}: nonempty strings (limits 4KiB/256B/64KiB)');
  }
}

function envelope(record) {
  return PREFIX + JSON.stringify({
    dedupKey: record.dedupKey, from: record.from, text: record.text,
    correlationToken: record.correlationToken,
  });
}

function atomicWrite(path, value) {
  const temporary = `${path}.${randomUUID()}.tmp`;
  let fd;
  try {
    fd = openSync(temporary, 'wx', 0o600);
    writeFileSync(fd, JSON.stringify(value) + '\n');
    fsyncSync(fd);
    closeSync(fd); fd = undefined;
    renameSync(temporary, path);
    const directory = openSync(dirname(path), 'r');
    try { fsyncSync(directory); } finally { closeSync(directory); }
  } finally {
    if (fd !== undefined) closeSync(fd);
    try { unlinkSync(temporary); } catch (e) { if (e.code !== 'ENOENT') throw e; }
  }
}

async function queue({ threadId, message }) {
  // No shell interpolation; never change/restart the app-server daemon here.
  const result = await exec('codex', ['queue', '--thread', threadId, '--message', message], {
    timeout: 30000, maxBuffer: 1024 * 1024,
  });
  return { stdout: result.stdout.trim() };
}

export function findRollout(threadId, sessionsDir = join(homedir(), '.codex', 'sessions')) {
  const found = [];
  function walk(dir) {
    for (const item of readdirSync(dir, { withFileTypes: true })) {
      const path = join(dir, item.name);
      if (item.isDirectory()) walk(path);
      else if (item.isFile() && item.name.endsWith(`-${threadId}.jsonl`)) found.push(path);
    }
  }
  walk(sessionsDir);
  if (found.length !== 1) throw new Error(`Expected one rollout for ${threadId}; found ${found.length}`);
  return found[0];
}

export class Inbound extends EventEmitter {
  constructor({ ledgerDir = join(HOME, 'bridge', 'ledger'), threadId,
    rolloutPath, enqueue = queue } = {}) {
    super();
    this.threadId = threadId ?? readFileSync(join(HOME, '.session-uuid'), 'utf8').trim();
    if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(this.threadId)) {
      throw new Error('threadId must be a pinned UUID, not a mutable session name');
    }
    this.ledgerDir = resolve(ledgerDir);
    this.rolloutPath = rolloutPath;
    this.enqueue = enqueue;
    mkdirSync(this.ledgerDir, { recursive: true, mode: 0o700 });
    const parent = openSync(dirname(this.ledgerDir), 'r');
    try { fsyncSync(parent); } finally { closeSync(parent); }
  }

  _path(key) { return join(this.ledgerDir, `${hash(key)}.json`); }

  _locked(fn) {
    // SQLite is ONLY the cross-process mutex. OS locks release on process death.
    // JSON records remain the source of truth. No await while holding this lock.
    const db = new DatabaseSync(join(this.ledgerDir, '.lock.sqlite'));
    try {
      db.exec('PRAGMA busy_timeout=10000; BEGIN IMMEDIATE');
      const result = fn();
      db.exec('COMMIT');
      return result;
    } finally { db.close(); }
  }

  _read(key) {
    let value;
    try { value = JSON.parse(readFileSync(this._path(key), 'utf8')); }
    catch (error) { if (error.code === 'ENOENT') return null; throw error; }
    validateMessage({ dedupKey: value.dedupKey, from: value.from, text: value.text });
    if (value.version !== 1 || value.dedupKey !== key || value.threadId !== this.threadId ||
        !states.has(value.state) || !nonempty(value.correlationToken) ||
        !Array.isArray(value.turnIds) || !value.turnIds.every(nonempty) ||
        !Number.isInteger(value.attempts) || value.attempts < 1 ||
        (value.state === 'completed') !== Boolean(value.commit) ||
        (value.commit && (value.commit.type !== 'committed' || value.commit.dedupKey !== key ||
          value.commit.threadId !== this.threadId || value.commit.commitId !== this._commitId(key) ||
          !value.turnIds.includes(value.commit.turnId)))) {
      throw new Error(`Invalid or wrong-thread ledger record: ${hash(key)}`);
    }
    return value;
  }

  _all() {
    return readdirSync(this.ledgerDir).filter(name => name.endsWith('.json')).map(name => {
      const raw = JSON.parse(readFileSync(join(this.ledgerDir, name), 'utf8'));
      if (!nonempty(raw.dedupKey) || name !== `${hash(raw.dedupKey)}.json`) {
        throw new Error(`Invalid ledger filename: ${name}`);
      }
      return this._read(raw.dedupKey);
    });
  }

  _commitId(key) { return hash(JSON.stringify([this.threadId, key])); }
  _write(record) { atomicWrite(this._path(record.dedupKey), record); }
  get(dedupKey) {
    if (!nonempty(dedupKey)) throw new Error('dedupKey required');
    return this._locked(() => this._read(dedupKey));
  }
  commits() { return this._locked(() => this._all().flatMap(r => r.commit ? [r.commit] : [])); }

  async deliver(message) {
    validateMessage(message);
    const prepared = this._locked(() => {
      let record = this._read(message.dedupKey);
      if (record && (record.from !== message.from || record.text !== message.text)) {
        throw new Error('dedupKey already belongs to different content or sender');
      }
      if (record && record.state !== 'confirmed-absent') return { record, enqueue: false };
      record ??= {
        version: 1, ...message, threadId: this.threadId, correlationToken: randomUUID(),
        turnIds: [], attempts: 0, createdAt: now(),
      };
      record.state = 'submitted';
      record.attempts++;
      record.attemptId = randomUUID();
      record.submittedAt = now();
      delete record.queueError;
      const encoded = envelope(record);
      if (Buffer.byteLength(encoded) > 96000) throw new Error('Encoded queue message exceeds 96KB');
      this._write(record); // Durable intent BEFORE the external submission.
      return { record, enqueue: true, message: encoded };
    });
    if (!prepared.enqueue) return { dedupKey: message.dedupKey, state: prepared.record.state,
      enqueued: false, commit: prepared.record.commit ?? null };
    try {
      await this.enqueue({ threadId: this.threadId, message: prepared.message });
      this._locked(() => {
        const record = this._read(message.dedupKey);
        if (record.attemptId === prepared.record.attemptId) {
          record.queueAcknowledgedAt = now(); this._write(record);
        }
      });
    } catch (failure) {
      this._locked(() => {
        const record = this._read(message.dedupKey);
        if (record.attemptId === prepared.record.attemptId) {
          record.queueError = String(failure.message).slice(0, 2048); this._write(record);
        }
      });
      // A CLI error/timeout does not prove non-acceptance. Leave submitted/running.
      throw new Error(`Queue outcome uncertain; retained ledger intent: ${failure.message}`, { cause: failure });
    }
    const record = this.get(message.dedupKey);
    return { dedupKey: record.dedupKey, state: record.state, enqueued: true, commit: record.commit ?? null };
  }

  confirmAbsent({ dedupKey, evidence, acceptDuplicateRisk }) {
    if (!nonempty(dedupKey) || !nonempty(evidence) || acceptDuplicateRisk !== true) {
      throw new Error('Explicit evidence and acceptDuplicateRisk:true required; timeouts are not proof');
    }
    return this._locked(() => {
      const record = this._read(dedupKey);
      if (!record) throw new Error('Unknown dedupKey');
      if (record.state === 'completed') return record;
      record.state = 'confirmed-absent';
      record.absence = { evidence, at: now(), acceptDuplicateRisk: true };
      this._write(record);
      return record;
    });
  }

  reconcile(rolloutPath = this.rolloutPath ?? findRollout(this.threadId)) {
    // Read-only, version-pinned adapter for Codex 0.154.0 rollout JSONL.
    // Ignore ONLY an unterminated tail; a corrupt complete line fails closed.
    const text = readFileSync(rolloutPath, 'utf8');
    const lines = text.slice(0, text.lastIndexOf('\n') + 1).split('\n').filter(Boolean);
    const events = lines.map(line => JSON.parse(line));
    const meta = events.find(e => e.type === 'session_meta');
    if (!meta || meta.payload?.id !== this.threadId) throw new Error('Rollout thread identity mismatch');
    const committed = this._locked(() => {
      const records = this._all();
      const byEnvelope = new Map(records.map(r => [envelope(r), r]));
      const byTurn = new Map();
      for (const r of records) for (const turn of r.turnIds) {
        if (byTurn.has(turn)) throw new Error('A turn maps to multiple dedupKeys');
        byTurn.set(turn, r);
      }
      const dirty = new Set();
      const notifications = [];
      let activeTurn = null;
      for (const e of events) {
        const p = e.payload;
        if (!object(p)) continue;
        if (e.type === 'event_msg' && p.type === 'task_started') {
          if (!nonempty(p.turn_id)) throw new Error('task_started lacks turn_id');
          activeTurn = p.turn_id;
        }
        if (e.type === 'response_item' && p.type === 'message' && p.role === 'user' && activeTurn) {
          for (const part of p.content ?? []) {
            const record = part.type === 'input_text' ? byEnvelope.get(part.text) : undefined;
            if (!record) continue;
            if (byTurn.has(activeTurn) && byTurn.get(activeTurn) !== record) {
              throw new Error('Multiple inbound messages merged into one turn; refusing commit');
            }
            byTurn.set(activeTurn, record);
            if (!record.turnIds.includes(activeTurn)) {
              record.turnIds.push(activeTurn);
              if (record.state !== 'completed') record.state = 'running';
              dirty.add(record);
            }
          }
        }
        if (e.type === 'event_msg' && p.type === 'task_complete') {
          if (!nonempty(p.turn_id)) throw new Error('task_complete lacks turn_id');
          const record = byTurn.get(p.turn_id);
          if (record && record.state !== 'completed') {
            record.state = 'completed';
            record.commit = { type: 'committed', dedupKey: record.dedupKey,
              commitId: this._commitId(record.dedupKey), threadId: this.threadId,
              turnId: p.turn_id, ts: now() };
            dirty.add(record); notifications.push(record.commit);
          }
          if (activeTurn === p.turn_id) activeTurn = null;
        }
        if (e.type === 'event_msg' && p.type === 'turn_aborted' && activeTurn === p.turn_id) activeTurn = null;
      }
      for (const record of dirty) this._write(record);
      return notifications;
    });
    // Event delivery cannot be atomic with a file commit. commits() is the recovery source.
    for (const event of committed) this.emit('committed', event);
    return committed;
  }

  watch({ pollMs = 1000 } = {}) {
    if (!Number.isInteger(pollMs) || pollMs < 100) throw new Error('pollMs must be >= 100');
    const poll = () => {
      try { this.reconcile(); } catch (error) { this.emit('watchError', error); }
    };
    poll();
    const timer = setInterval(poll, pollMs);
    return () => clearInterval(timer);
  }
}

export const createInbound = options => new Inbound(options);
let singleton;
export async function deliver(message) { singleton ??= createInbound(); return singleton.deliver(message); }

async function main() {
  const [command = 'serve', ...args] = process.argv.slice(2);
  const options = {};
  for (let i = 0; i < args.length; i += 2) {
    const key = { '--ledger-dir': 'ledgerDir', '--thread': 'threadId', '--rollout': 'rolloutPath' }[args[i]];
    if (!key || !args[i + 1]) throw new Error('Options: --ledger-dir PATH --thread UUID --rollout PATH');
    options[key] = args[i + 1];
  }
  if (!['serve', 'watch', 'deliver', 'commits', 'status', 'confirm-absent', 'scan'].includes(command)) {
    throw new Error('Commands: serve | watch | deliver | commits | status | confirm-absent | scan');
  }
  const receiver = createInbound(options);
  const output = value => process.stdout.write(JSON.stringify(value) + '\n');
  receiver.on('committed', output);
  receiver.on('watchError', error => console.error(`inbound watcher: ${error.message}`));
  if (command === 'commits') { receiver.commits().forEach(output); return; }
  if (command === 'scan') { receiver.reconcile(); return; }
  if (command === 'watch') {
    receiver.commits().forEach(output);
    const stop = receiver.watch();
    for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => { stop(); process.exit(0); });
    return;
  }
  if (command !== 'serve') {
    let input = '';
    for await (const chunk of process.stdin) {
      input += chunk;
      if (Buffer.byteLength(input) > 1024 * 1024) throw new Error('Input too large');
    }
    const params = JSON.parse(input);
    const result = command === 'deliver' ? await receiver.deliver(params)
      : command === 'status' ? receiver.get(params.dedupKey) : receiver.confirmAbsent(params);
    output({ type: 'result', result });
    return;
  }
  // JSONL commands and asynchronous committed events share stdout; stderr is diagnostics.
  receiver.commits().forEach(output);
  const stop = receiver.watch();
  try {
    for await (const line of createInterface({ input: process.stdin, crlfDelay: Infinity })) {
      let request;
      try {
        if (Buffer.byteLength(line) > 1024 * 1024) throw new Error('Input too large');
        request = JSON.parse(line);
        if (!object(request) || !['deliver', 'status', 'commits', 'confirm-absent'].includes(request.method)) {
          throw new Error('Unknown method');
        }
        const result = request.method === 'deliver' ? await receiver.deliver(request.params)
          : request.method === 'status' ? receiver.get(request.params?.dedupKey)
          : request.method === 'commits' ? receiver.commits() : receiver.confirmAbsent(request.params ?? {});
        output({ type: 'result', id: request.id ?? null, result });
      } catch (error) { output({ type: 'error', id: request?.id ?? null, error: error.message }); }
    }
  } finally { stop(); }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => { console.error(error.message); process.exitCode = 1; });
}
