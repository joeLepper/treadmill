import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, readdir, rm, symlink, utimes, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { Readable, Writable } from 'node:stream';
import { createTools, serve } from './msg-server.mjs';
import { reapOrphans } from './outbound-next.mjs';

const root = await mkdtemp(join(tmpdir(), 'fran-msg-test-'));
try {
  await writeFile(join(root, 'peers.json'), JSON.stringify(['alan', 'donna']));
  const tools = createTools(root);
  assert.deepEqual(await tools.listPeers(), ['alan', 'donna']);
  const text = 'Hello Alan.\nUnicode: orange 🍊';
  const { id } = await tools.sendMessage({ to: 'alan', text });
  assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  const message = JSON.parse(await readFile(join(root, 'outbox', `${id}.json`), 'utf8'));
  assert.deepEqual(message, { id, ts: message.ts, from: 'gerald', to: 'alan', text });
  assert.equal(new Date(message.ts).toISOString(), message.ts);
  assert.deepEqual(await readdir(join(root, 'outbox')), [`${id}.json`]);
  for (const args of [null, {}, { to: 'unknown', text }, { to: '../alan', text },
    { to: 'alan', text: ' ' }, { to: 'alan', text: 7 }, { to: 'alan', text, from: 'joe' }]) {
    await assert.rejects(tools.sendMessage(args));
  }
  assert.deepEqual(await readdir(join(root, 'outbox')), [`${id}.json`]);
  await writeFile(join(root, 'peers.json'), '{}');
  await assert.rejects(tools.listPeers());
  await assert.rejects(tools.sendMessage({ to: 'alan', text }));
  await writeFile(join(root, 'peers.json'), '["alan"]');

  // Exercise MCP framing, initialization, tools, bad input, and notification safety.
  const request = (id, method, params) => ({ jsonrpc: '2.0', id, method, params });
  const frames = [
    '{broken',
    request(0, 'tools/call', { name: 'send_message', arguments: { to: 'alan', text } }),
    request(1, 'initialize', { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'test', version: '1' } }),
    { jsonrpc: '2.0', method: 'notifications/initialized' },
    request(2, 'tools/list'),
    request(3, 'tools/call', { name: 'list_peers', arguments: {} }),
    request(4, 'tools/call', { name: 'send_message', arguments: { to: 'alan', text } }),
    request(5, 'tools/call', { name: 'send_message', arguments: { to: 'nope', text } }),
    { jsonrpc: '2.0', method: 'tools/call', params: { name: 'send_message', arguments: { to: 'alan', text } } },
  ];
  let output = '';
  await serve(Readable.from(frames.map(f => (typeof f === 'string' ? f : JSON.stringify(f)) + '\n')),
    new Writable({ write(chunk, encoding, done) { output += chunk; done(); } }), root);
  const replies = output.trim().split('\n').map(JSON.parse);
  assert.equal(replies.length, 7);
  assert.equal(replies[0].error.code, -32700);
  assert.ok(replies[1].error);
  assert.equal(replies[2].result.protocolVersion, '2025-06-18');
  assert.deepEqual(replies[3].result.tools.map(t => t.name), ['send_message', 'list_peers']);
  assert.deepEqual(JSON.parse(replies[4].result.content[0].text), ['alan']);
  const sent = JSON.parse(replies[5].result.content[0].text);
  assert.equal(JSON.parse(await readFile(join(root, 'outbox', `${sent.id}.json`), 'utf8')).text, text);
  assert.equal(replies[6].result.isError, true);
  assert.equal((await readdir(join(root, 'outbox'))).length, 2);

  // Finding A (ADR-0102): reapOrphans recovers an orphan <id>.json.tmp left by a
  // crash between fsync and rename, and never touches an in-flight write.
  {
    const ob = join(root, 'reap-outbox');
    await mkdir(ob, { recursive: true });
    const u = (c) => `${c}0000000-0000-4000-8000-000000000000`;
    const complete = u('a'), fresh = u('b'), dup = u('c'), partial = u('d');
    const line = (mid, to) => JSON.stringify({ id: mid, ts: new Date().toISOString(), from: 'gerald', to, text: 'hi' }) + '\n';
    await writeFile(join(ob, `${complete}.json.tmp`), line(complete, 'alan')); // stale complete -> promote
    await writeFile(join(ob, `${fresh}.json.tmp`), line(fresh, 'alan'));        // fresh -> leave (in-flight)
    await writeFile(join(ob, `${dup}.json`), line(dup, 'alan'));                // already spooled
    await writeFile(join(ob, `${dup}.json.tmp`), line(dup, 'alan'));            // stale dup -> unlink
    await writeFile(join(ob, `${partial}.json.tmp`), '{ partial');             // stale garbage -> unlink
    const old = new Date(Date.now() - 120_000);
    for (const f of [`${complete}.json.tmp`, `${dup}.json.tmp`, `${partial}.json.tmp`]) await utimes(join(ob, f), old, old);
    await reapOrphans(ob);
    assert.deepEqual((await readdir(ob)).sort(), [`${complete}.json`, `${dup}.json`, `${fresh}.json.tmp`].sort());
    assert.equal(JSON.parse(await readFile(join(ob, `${complete}.json`), 'utf8')).to, 'alan');
  }

  // The CLI main-guard must fire through a symlink (the ADR-0104 follow-up symlinks
  // bridge code to a canonical checkout). A no-arg run prints usage and exits 2; a
  // guard using resolve() instead of realpath would silently exit 0 through a link.
  {
    const linkDir = await mkdtemp(join(tmpdir(), 'reap-link-'));
    const real = fileURLToPath(new URL('./outbound-next.mjs', import.meta.url));
    const via = join(linkDir, 'outbound-next.mjs');
    await symlink(real, via);
    const exitCode = (p) => new Promise((res) => spawn(process.execPath, [p], { stdio: 'ignore' }).on('exit', res));
    assert.equal(await exitCode(real), 2); // direct invocation runs the CLI
    assert.equal(await exitCode(via), 2);  // symlinked invocation must too
    await rm(linkDir, { recursive: true, force: true });
  }

  // A broken spool must return an error, never a successful message id.
  await rm(join(root, 'outbox'), { recursive: true });
  await writeFile(join(root, 'outbox'), 'not a directory');
  await assert.rejects(tools.sendMessage({ to: 'alan', text }));
  console.log('PASS: spool JSON, input refusal, MCP lifecycle, notifications, write failure, and orphan-tmp reaper');
} finally {
  await rm(root, { recursive: true, force: true });
}
