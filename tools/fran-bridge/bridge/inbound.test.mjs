import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync, writeFileSync, appendFileSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createInbound } from './inbound.mjs';

const THREAD = '00000000-0000-4000-8000-000000000001';
const modulePath = fileURLToPath(new URL('./inbound.mjs', import.meta.url));
const message = { dedupKey: 'A/opaque-key', from: 'alan', text: 'A test message\nwith Unicode 🍊' };
const evt = payload => ({ type: 'event_msg', payload });
const user = text => ({ type: 'response_item', payload: { type: 'message', role: 'user',
  content: [{ type: 'input_text', text }] } });

function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), 'fran-inbound-test-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const ledgerDir = join(root, 'ledger'), rolloutPath = join(root, 'rollout.jsonl');
  writeFileSync(rolloutPath, JSON.stringify({ type: 'session_meta', payload: { id: THREAD } }) + '\n');
  const submissions = [];
  const options = { ledgerDir, rolloutPath, threadId: THREAD,
    enqueue: async value => { submissions.push(value); } };
  const append = (...events) => appendFileSync(rolloutPath, events.map(e => JSON.stringify(e) + '\n').join(''));
  return { root, options, submissions, append, receiver: createInbound(options) };
}

function started(f, id = 'turn-1', text = f.submissions[0].message) {
  f.append(evt({ type: 'task_started', turn_id: id }), user(text));
}
function completed(f, id = 'turn-1') { f.append(evt({ type: 'task_complete', turn_id: id })); }

test('submitted/running redelivery never enqueues; completed redelivery is a no-op', async t => {
  const f = fixture(t), seen = [];
  f.receiver.on('committed', event => seen.push(event));
  assert.equal((await f.receiver.deliver(message)).state, 'submitted');
  assert.equal((await f.receiver.deliver(message)).enqueued, false);
  started(f);
  assert.deepEqual(f.receiver.reconcile(), []);
  assert.equal(f.receiver.get(message.dedupKey).state, 'running');
  assert.equal((await f.receiver.deliver(message)).enqueued, false);
  completed(f);
  const [commit] = f.receiver.reconcile();
  assert.equal(commit.type, 'committed');
  assert.equal(commit.dedupKey, message.dedupKey);
  assert.equal(f.receiver.get(message.dedupKey).state, 'completed');
  assert.equal((await f.receiver.deliver(message)).enqueued, false);
  assert.deepEqual(f.receiver.reconcile(), []);
  assert.equal(seen.length, 1);
  assert.equal(f.submissions.length, 1);
  assert.deepEqual(f.receiver.commits(), [commit]);
});

test('unknown queue outcome is retained; absence requires explicit recovery decision', async t => {
  const f = fixture(t);
  const r = createInbound({ ...f.options, enqueue: async () => { throw new Error('timeout after accept'); } });
  await assert.rejects(r.deliver(message), /uncertain/);
  assert.equal(r.get(message.dedupKey).state, 'submitted');
  assert.equal((await f.receiver.deliver(message)).enqueued, false);
  assert.equal(f.submissions.length, 0);
  assert.throws(() => r.confirmAbsent({ dedupKey: message.dedupKey, evidence: 'timeout' }), /Explicit/);
  r.confirmAbsent({ dedupKey: message.dedupKey, evidence: 'Operator authorized recovery after inspecting the stopped queue', acceptDuplicateRisk: true });
  assert.equal(r.get(message.dedupKey).state, 'confirmed-absent');
  await f.receiver.deliver(message);
  assert.equal(f.submissions.length, 1);
  assert.equal(r.get(message.dedupKey).attempts, 2);
});

test('duplicate executions produce only one durable commit', async t => {
  const f = fixture(t);
  await f.receiver.deliver(message);
  started(f, 'turn-first'); completed(f, 'turn-first');
  started(f, 'turn-duplicate'); completed(f, 'turn-duplicate');
  assert.equal(f.receiver.reconcile().length, 1);
  assert.equal(f.receiver.commits().length, 1);
  assert.equal(f.receiver.commits()[0].turnId, 'turn-first');
  assert.deepEqual(f.receiver.get(message.dedupKey).turnIds, ['turn-first', 'turn-duplicate']);
});

test('historical starts do not undo an explicit recovery decision', async t => {
  const f = fixture(t);
  await f.receiver.deliver(message); started(f, 'aborted-old');
  f.append(evt({ type: 'turn_aborted', turn_id: 'aborted-old' }));
  f.receiver.reconcile();
  f.receiver.confirmAbsent({ dedupKey: message.dedupKey,
    evidence: 'Operator accepted replay after observing abort', acceptDuplicateRisk: true });
  f.receiver.reconcile();
  assert.equal(f.receiver.get(message.dedupKey).state, 'confirmed-absent');
  await f.receiver.deliver(message);
  assert.equal(f.submissions.length, 2);
  f.receiver.reconcile();
  assert.equal(f.receiver.get(message.dedupKey).state, 'submitted');
});

test('fail closed on key reuse, malformed input, corrupted ledger, or wrong thread', async t => {
  const f = fixture(t);
  await f.receiver.deliver(message);
  await assert.rejects(f.receiver.deliver({ ...message, text: 'different' }), /different content/);
  await assert.rejects(f.receiver.deliver({ ...message, from: 'donna' }), /different content/);
  await assert.rejects(f.receiver.deliver({ ...message, dedupKey: ' ' }), /Expected/);
  const other = createInbound({ ...f.options, threadId: '00000000-0000-4000-8000-000000000002' });
  await assert.rejects(other.deliver(message), /wrong-thread/);
  assert.throws(() => other.reconcile(), /identity mismatch/);
  const record = readdirSync(f.options.ledgerDir).find(n => n.endsWith('.json'));
  writeFileSync(join(f.options.ledgerDir, record), '{corrupt');
  await assert.rejects(f.receiver.deliver(message));
  assert.equal(f.submissions.length, 1);
});

test('only a matching user input and successful task_complete can commit', async t => {
  const f = fixture(t);
  await f.receiver.deliver(message);
  // Text quoted in assistant output or a wrong-thread completion is not evidence.
  f.append(evt({ type: 'task_started', turn_id: 'unrelated' }),
    { type: 'response_item', payload: { type: 'message', role: 'assistant', content: [{ type: 'input_text', text: f.submissions[0].message }] } });
  completed(f, 'unrelated');
  assert.deepEqual(f.receiver.reconcile(), []);
  started(f, 'aborted');
  f.append(evt({ type: 'turn_aborted', turn_id: 'aborted' }));
  assert.deepEqual(f.receiver.reconcile(), []);
  assert.equal(f.receiver.commits().length, 0);
  started(f, 'successful');
  const tail = JSON.stringify(evt({ type: 'task_complete', turn_id: 'successful' }));
  appendFileSync(f.options.rolloutPath, tail);
  assert.equal(f.receiver.reconcile().length, 0, 'unterminated tail must not commit');
  appendFileSync(f.options.rolloutPath, '\n');
  assert.equal(f.receiver.reconcile().length, 1);
});

test('merged inbound messages fail closed rather than ack both as separate turns', async t => {
  const f = fixture(t);
  await f.receiver.deliver(message);
  await f.receiver.deliver({ ...message, dedupKey: 'B' });
  started(f, 'merged');
  f.append(user(f.submissions[1].message)); completed(f, 'merged');
  assert.throws(() => f.receiver.reconcile(), /merged/);
  assert.equal(f.receiver.commits().length, 0);
});

test('actual process exit after submission: restart adopts turn, redelivery commits once', async t => {
  const f = fixture(t);
  const code = `
    import {createInbound} from ${JSON.stringify(modulePath)};
    import {appendFileSync} from 'node:fs';
    const options = ${JSON.stringify({ ...f.options, enqueue: undefined })};
    const r = createInbound({...options, enqueue: async ({message}) => {
      for (const e of [${JSON.stringify(evt({ type: 'task_started', turn_id: 'crashed-turn' }))},
        {type:'response_item',payload:{type:'message',role:'user',content:[{type:'input_text',text:message}]}}])
        appendFileSync(options.rolloutPath,JSON.stringify(e)+'\\n');
      process.exit(97); // accepted and started, no receipt/turn-id persisted in ledger
    }});
    await r.deliver(${JSON.stringify(message)});
  `;
  const child = spawnSync(process.execPath, ['--input-type=module', '-e', code], { encoding: 'utf8' });
  assert.equal(child.status, 97, child.stderr);
  const restarted = createInbound(f.options);
  assert.equal(restarted.get(message.dedupKey).state, 'submitted');
  assert.equal((await restarted.deliver(message)).enqueued, false);
  restarted.reconcile();
  assert.equal(restarted.get(message.dedupKey).state, 'running');
  completed(f, 'crashed-turn');
  assert.equal(restarted.reconcile().length, 1);
  assert.equal(f.submissions.length, 0);
  const verify = spawnSync(process.execPath, ['--input-type=module', '-e', `
    import {createInbound} from ${JSON.stringify(modulePath)};
    const r=createInbound(${JSON.stringify({ ...f.options, enqueue: undefined })});
    if(r.get(${JSON.stringify(message.dedupKey)}).state!=='completed')process.exit(2);
    if(r.reconcile().length!==0 || r.commits().length!==1)process.exit(3);
    console.log(r.commits()[0].commitId);
  `], { encoding: 'utf8' });
  assert.equal(verify.status, 0, verify.stderr);
  assert.equal(verify.stdout.trim(), restarted.commits()[0].commitId);
});

test('crash after durable commit but before event delivery is recoverable through commits()', async t => {
  const f = fixture(t);
  await f.receiver.deliver(message); started(f); completed(f);
  const child = spawnSync(process.execPath, ['--input-type=module', '-e', `
    import {createInbound} from ${JSON.stringify(modulePath)};
    const r=createInbound(${JSON.stringify({ ...f.options, enqueue: undefined })});
    r.on('committed',()=>process.exit(98));
    r.reconcile();
  `], { encoding: 'utf8' });
  assert.equal(child.status, 98, child.stderr);
  assert.equal(f.receiver.get(message.dedupKey).state, 'completed');
  assert.equal(f.receiver.commits().length, 1);
  assert.deepEqual(f.receiver.reconcile(), []);
  const cli = spawnSync(process.execPath, [modulePath, 'commits', '--ledger-dir', f.options.ledgerDir,
    '--thread', THREAD], { encoding: 'utf8' });
  assert.equal(cli.status, 0, cli.stderr);
  assert.deepEqual(JSON.parse(cli.stdout), f.receiver.commits()[0]);
});

test('two processes racing deliver enqueue only once', async t => {
  const f = fixture(t), counter = join(f.root, 'calls.jsonl');
  const code = `
    import {createInbound} from ${JSON.stringify(modulePath)};
    import {appendFileSync} from 'node:fs';
    const r=createInbound({...${JSON.stringify({ ...f.options, enqueue: undefined })},
      enqueue:async()=>{appendFileSync(${JSON.stringify(counter)},'queued\\n')}});
    await r.deliver(${JSON.stringify(message)});
  `;
  const run = () => new Promise((resolve, reject) => {
    const p = spawn(process.execPath, ['--input-type=module', '-e', code], { stdio: ['ignore', 'pipe', 'pipe'] });
    let stderr = ''; p.stderr.on('data', data => stderr += data);
    p.on('error', reject); p.on('exit', code => code === 0 ? resolve() : reject(new Error(stderr)));
  });
  await Promise.all([run(), run()]);
  assert.equal(readFileSync(counter, 'utf8'), 'queued\n');
  assert.equal(f.receiver.get(message.dedupKey).attempts, 1);
});

test('serve emits commit separately from replies and replays it with a stable ID', async t => {
  const f = fixture(t);
  await f.receiver.deliver(message); started(f); completed(f);
  const args = [modulePath, 'serve', '--ledger-dir', f.options.ledgerDir,
    '--thread', THREAD, '--rollout', f.options.rolloutPath];
  const input = JSON.stringify({ id: 'status-1', method: 'status', params: { dedupKey: message.dedupKey } }) + '\n';
  const run = () => {
    const child = spawnSync(process.execPath, args, { encoding: 'utf8', input: input + '{bad-json\n' });
    assert.equal(child.status, 0, child.stderr);
    return child.stdout.trim().split('\n').map(JSON.parse);
  };
  const first = run();
  assert.equal(first.length, 3);
  assert.equal(first[0].type, 'committed');
  assert.equal(first[1].type, 'result');
  assert.equal(first[1].id, 'status-1');
  assert.equal(first[1].result.state, 'completed');
  assert.equal(first[2].type, 'error');
  assert.deepEqual(run()[0], first[0]);
});
