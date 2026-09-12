import { randomUUID } from 'node:crypto';
import { mkdir, open, readFile, rename, unlink } from 'node:fs/promises';
import { realpathSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';

const home = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const nonempty = value => typeof value === 'string' && value.trim().length > 0;

// A separate root lets tests exercise the real implementation without sending mail.
export function createTools(root = home) {
  async function listPeers() {
    const peers = JSON.parse(await readFile(join(root, 'peers.json'), 'utf8'));
    if (!Array.isArray(peers) || !peers.every(nonempty) || new Set(peers).size !== peers.length) {
      throw new Error('peers.json must contain an array of unique nonempty strings');
    }
    return peers;
  }

  async function sendMessage(args) {
    if (!object(args) || Object.keys(args).some(k => !['to', 'text'].includes(k)) ||
        !nonempty(args.to) || !nonempty(args.text)) {
      throw new Error('Expected only nonempty string fields: to, text');
    }
    if (!(await listPeers()).includes(args.to)) throw new Error('Unknown peer');
    const id = randomUUID();
    const message = { id, ts: new Date().toISOString(), from: 'fran', to: args.to, text: args.text };
    const outbox = join(root, 'outbox');
    await mkdir(outbox, { recursive: true, mode: 0o700 });
    const temporary = join(outbox, `${id}.json.tmp`);
    const destination = join(outbox, `${id}.json`);
    let handle;
    let created = false;
    try {
      handle = await open(temporary, 'wx', 0o600);
      created = true;
      await handle.writeFile(JSON.stringify(message) + '\n', 'utf8');
      await handle.sync();
      await handle.close();
      handle = undefined;
      await rename(temporary, destination);
    } catch (error) {
      await handle?.close().catch(() => {});
      if (created) await unlink(temporary).catch(() => {});
      throw error;
    }
    return { id };
  }
  return { listPeers, sendMessage };
}

const toolDefinitions = [
  {
    name: 'send_message',
    description: 'Queue an outbound message from Fran to a fleet peer. Success means spooled, not delivered. On a lost response (e.g. "Transport closed") the message is usually already spooled — do not blind-retry; check the outbox first.',
    inputSchema: {
      type: 'object', properties: { to: { type: 'string', minLength: 1 }, text: { type: 'string', minLength: 1 } },
      required: ['to', 'text'], additionalProperties: false,
    },
  },
  {
    name: 'list_peers', description: 'List the allowed recipients from Fran’s peers.json.',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false },
  },
];

export async function serve(input = process.stdin, output = process.stdout, root = home) {
  const tools = createTools(root);
  // A dead parent (a daemon restart) destroys stdout; without this, reply()'s bare
  // output.write throws an unhandled EPIPE and the MCP client reports the scary
  // "Transport closed" instead of a clean stop. End the read loop on any stdout
  // error so serve() returns cleanly (ADR-0102).
  output.on('error', () => { try { input.destroy(); } catch { /* ignore */ } });
  let initialized = false;
  let ready = false;
  const reply = message => output.write(JSON.stringify({ jsonrpc: '2.0', ...message }) + '\n');
  const error = (id, code, message) => reply({ id, error: { code, message } });
  for await (const line of createInterface({ input, crlfDelay: Infinity })) {
    let request;
    try { request = JSON.parse(line); }
    catch { error(null, -32700, 'Parse error'); continue; }
    if (!object(request) || request.jsonrpc !== '2.0' || typeof request.method !== 'string' ||
        ('id' in request && !(typeof request.id === 'string' || Number.isInteger(request.id)))) {
      error(null, -32600, 'Invalid request'); continue;
    }
    // Notifications must never execute a tool or receive a response.
    if (!('id' in request)) {
      if (request.method === 'notifications/initialized' && initialized) ready = true;
      continue;
    }
    const { id, method, params } = request;
    if (method === 'initialize') {
      if (initialized || !object(params) || typeof params.protocolVersion !== 'string' ||
          !object(params.capabilities) || !object(params.clientInfo)) {
        error(id, -32602, 'Invalid initialization'); continue;
      }
      initialized = true;
      const supported = ['2024-11-05', '2025-03-26', '2025-06-18'];
      reply({ id, result: {
        protocolVersion: supported.includes(params.protocolVersion) ? params.protocolVersion : '2025-06-18',
        capabilities: { tools: {} }, serverInfo: { name: 'fran-msg', version: '1.0.0' },
      } });
      continue;
    }
    if (method === 'ping') { reply({ id, result: {} }); continue; }
    if (!ready) { error(id, -32600, 'Initialize before using tools'); continue; }
    if (method === 'tools/list') { reply({ id, result: { tools: toolDefinitions } }); continue; }
    if (method !== 'tools/call') { error(id, -32601, 'Method not found'); continue; }
    if (!object(params) || !toolDefinitions.some(t => t.name === params.name)) {
      error(id, -32602, 'Unknown tool'); continue;
    }
    try {
      let result;
      if (params.name === 'send_message') result = await tools.sendMessage(params.arguments);
      else {
        const args = params.arguments === undefined ? {} : params.arguments;
        if (!object(args) || Object.keys(args).length) throw new Error('list_peers takes no arguments');
        result = await tools.listPeers();
      }
      reply({ id, result: { content: [{ type: 'text', text: JSON.stringify(result) }] } });
    } catch (failure) {
      reply({ id, result: { isError: true, content: [{ type: 'text', text: failure.message }] } });
    }
  }
}

// Realpath both sides so a symlinked invocation (ADR-0104 canonical-checkout
// follow-up) still starts the server; resolve() alone does not follow symlinks.
if (process.argv[1] && realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url))) {
  serve().catch(error => { console.error(error.message); process.exitCode = 1; });
}
