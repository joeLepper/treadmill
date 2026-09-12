// Regression test for the gerald_msg MCP spawn path (ADR-0104).
//
// Codex resolves the `command` in [mcp_servers.gerald_msg] against the env of the
// app-server daemon, which inherits PATH from gerald-supervise.sh. `node` comes
// from asdf, so that PATH must include $HOME/.asdf/shims. When it did not, node
// was ENOENT, the MCP server never started, and send_message/list_peers were
// silently absent from Gerald's session — he could not reply to the bus at all.
//
// Run: node bridge/supervise-path.test.mjs

import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const run = promisify(execFile);
const tool = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const supervise = join(tool, 'session', 'gerald-supervise.sh');
const config = join(tool, 'codex', 'isolated-home-config.toml');

// A synthetic HOME whose only node lives in the asdf shims dir, so resolution
// proves the script's PATH reaches asdf rather than the test runner's PATH.
const home = await mkdtemp(join(tmpdir(), 'gerald-path-test-'));
try {
  await mkdir(join(home, '.asdf', 'shims'), { recursive: true });
  await mkdir(join(home, '.local', 'bin'), { recursive: true });
  await writeFile(join(home, '.asdf', 'shims', 'node'), '#!/bin/sh\necho shim-node\n', { mode: 0o755 });
  await writeFile(join(home, '.local', 'bin', 'codex'), '#!/bin/sh\necho codex\n', { mode: 0o755 });

  const script = await readFile(supervise, 'utf8');
  const pathLine = script.split('\n').find(line => line.startsWith('export PATH='));
  assert.ok(pathLine, 'gerald-supervise.sh must export PATH for the daemon');
  assert.match(pathLine, /\$HOME\/\.asdf\/shims/,
    'daemon PATH must include $HOME/.asdf/shims or the gerald_msg `node` spawn is ENOENT');

  // The MCP server is launched as a bare `node`, so PATH resolution is load-bearing.
  const toml = await readFile(config, 'utf8');
  const section = toml.split('[mcp_servers.gerald_msg]')[1];
  assert.ok(section, 'config must define [mcp_servers.gerald_msg]');
  assert.match(section.split('\n[')[0], /command\s*=\s*"node"/,
    'gerald_msg is spawned as `node`; the supervise PATH must make it resolvable');

  const resolveNode = async (line) => {
    const { stdout } = await run('/bin/bash', ['-c', `${line}; command -v node || true`],
      { env: { HOME: home, PATH: '/usr/bin:/bin' } });
    return stdout.trim();
  };

  assert.equal(await resolveNode(pathLine), join(home, '.asdf', 'shims', 'node'),
    'the shipped PATH line must resolve node to the asdf shim');

  // Foil: the pre-fix PATH line must NOT resolve node, so a revert fails this test.
  assert.equal(await resolveNode('export PATH="$HOME/.local/bin:$PATH"'), '',
    'expected the old PATH line to leave node unresolvable (regression foil)');

  console.log('PASS: supervise PATH resolves node for the gerald_msg MCP spawn');
} finally {
  await rm(home, { recursive: true, force: true });
}
