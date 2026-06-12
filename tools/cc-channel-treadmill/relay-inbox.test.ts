/**
 * Tests for the transactional relay inbox (task ecd6d6eb — the
 * 2026-06-12 drain-without-delivery race).
 *
 * Real files in real tmp dirs; the notifier is the only injected seam
 * (scripted to succeed, fail, or record), matching the established
 * real-IO test convention. The fs.watch wiring stays in
 * treadmill-events.ts — watch event ordering is OS-dependent and the
 * inbox's processFile/drain surface is exactly what the watcher calls.
 */
import { afterEach, expect, test } from 'bun:test'

import { mkdirSync, mkdtempSync, rmSync, writeFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { RelayInbox } from './relay-inbox.ts'

const tmpDirs: string[] = []

function makeInboxDir(): string {
  const dir = mkdtempSync(join(tmpdir(), 'relay-inbox-test-'))
  tmpDirs.push(dir)
  mkdirSync(join(dir, 'worker'), { recursive: true })
  return dir
}

afterEach(() => {
  while (tmpDirs.length) rmSync(tmpDirs.pop()!, { recursive: true, force: true })
})

type Delivery = { content: string; meta: Record<string, string> }

function recordingNotify(deliveries: Delivery[], failTimes = 0) {
  let failures = failTimes
  return async (content: string, meta: Record<string, string>) => {
    if (failures > 0) {
      failures -= 1
      throw new Error('notification landed nowhere (resume window)')
    }
    deliveries.push({ content, meta })
  }
}

// ── THE RACE (control + fix) ────────────────────────────────────────────────

test('CONTROL — the pre-fix unlink-before-notify shape loses the message', async () => {
  // The old processRelayFile, reproduced verbatim in miniature: read,
  // UNLINK, then notify. When the notify lands nowhere (the resume
  // window), the file is already gone — countermand lost, zero files
  // left on disk: exactly the live incident's observed state.
  const dir = makeInboxDir()
  const fpath = join(dir, '1-countermand.md')
  writeFileSync(fpath, '[from: coordinator] COUNTERMAND')
  const deliveries: Delivery[] = []
  const notify = recordingNotify(deliveries, 1) // resume window: send lost

  const { readFile, unlink } = await import('node:fs/promises')
  const oldProcessRelayFile = async () => {
    const content = await readFile(fpath, 'utf-8')
    await unlink(fpath) // ← the defect: destructive before delivery
    await notify(content, { source: 'relay' })
  }
  await oldProcessRelayFile().catch(() => {})

  expect(deliveries).toEqual([]) // never delivered…
  expect(existsSync(fpath)).toBe(false) // …and gone from disk. LOST.
})

test('FIX — a failed notify RETAINS the file; the resweep delivers it', async () => {
  const dir = makeInboxDir()
  const fpath = join(dir, '1-countermand.md')
  writeFileSync(fpath, '[from: coordinator] COUNTERMAND')
  const deliveries: Delivery[] = []
  const inbox = new RelayInbox(dir, recordingNotify(deliveries, 1), {
    log: () => {},
  })

  await inbox.markAttached() // first drain: notify fails (resume window)
  expect(deliveries).toEqual([])
  expect(existsSync(fpath)).toBe(true) // retained — the durable leg held

  await inbox.drain() // the resweep (or next restart) retries
  expect(deliveries.length).toBe(1)
  expect(deliveries[0].content).toContain('COUNTERMAND')
  expect(existsSync(fpath)).toBe(false) // unlink only after delivery
})

// ── attach gating ───────────────────────────────────────────────────────────

test('nothing is drained or delivered before the session attaches', async () => {
  const dir = makeInboxDir()
  writeFileSync(join(dir, 'a.md'), 'queued while down')
  const deliveries: Delivery[] = []
  const inbox = new RelayInbox(dir, recordingNotify(deliveries))

  await inbox.drain() // pre-attach drain is a no-op
  await inbox.processFile(join(dir, 'a.md'), null) // pre-attach watch event
  expect(deliveries).toEqual([])
  expect(existsSync(join(dir, 'a.md'))).toBe(true)

  await inbox.markAttached()
  expect(deliveries.length).toBe(1)
  expect(existsSync(join(dir, 'a.md'))).toBe(false)
})

test('markAttached drains base dir AND subfolders, with subfolder meta', async () => {
  const dir = makeInboxDir()
  writeFileSync(join(dir, 'base.md'), 'base message')
  writeFileSync(join(dir, 'worker', 'sub.md'), 'worker-routed message')
  const deliveries: Delivery[] = []
  const inbox = new RelayInbox(dir, recordingNotify(deliveries), {
    subfolders: ['worker'],
  })

  await inbox.markAttached()

  expect(deliveries.length).toBe(2)
  const bySub = Object.fromEntries(
    deliveries.map(d => [d.meta.subfolder ?? 'base', d]),
  )
  expect(bySub.base.meta).toEqual({ source: 'relay' })
  expect(bySub.worker.meta).toEqual({ source: 'relay', subfolder: 'worker' })
})

test('markAttached is idempotent (no double drain)', async () => {
  const dir = makeInboxDir()
  writeFileSync(join(dir, 'a.md'), 'once')
  const deliveries: Delivery[] = []
  const inbox = new RelayInbox(dir, recordingNotify(deliveries))

  await inbox.markAttached()
  await inbox.markAttached()

  expect(deliveries.length).toBe(1)
})

// ── duplicate suppression / hygiene ─────────────────────────────────────────

test('concurrent processFile calls for one file deliver exactly once', async () => {
  // fs.watch fires multiple events per file; the in-flight set replaces
  // the old unlink-first idempotency without its message-loss cost.
  const dir = makeInboxDir()
  const fpath = join(dir, 'a.md')
  const deliveries: Delivery[] = []
  let release: () => void = () => {}
  const gate = new Promise<void>(r => (release = r))
  const inbox = new RelayInbox(dir, async (content, meta) => {
    await gate // hold the first delivery open while the dup arrives
    deliveries.push({ content, meta })
  })
  // Attach on an empty dir FIRST — otherwise the attach-drain itself
  // consumes the file while the gate is closed and deadlocks the test.
  await inbox.markAttached()
  writeFileSync(fpath, 'dup-prone')

  const first = inbox.processFile(fpath, null)
  const second = inbox.processFile(fpath, null) // watch dup while in flight
  release()
  await Promise.all([first, second])

  expect(deliveries.length).toBe(1)
})

test('non-md files are ignored by drain', async () => {
  const dir = makeInboxDir()
  writeFileSync(join(dir, 'notes.txt'), 'not a relay file')
  const deliveries: Delivery[] = []
  const inbox = new RelayInbox(dir, recordingNotify(deliveries))

  await inbox.markAttached()

  expect(deliveries).toEqual([])
  expect(existsSync(join(dir, 'notes.txt'))).toBe(true)
})

test('a vanished file (competing consumer) is skipped quietly', async () => {
  const dir = makeInboxDir()
  const deliveries: Delivery[] = []
  const inbox = new RelayInbox(dir, recordingNotify(deliveries))
  await inbox.markAttached()

  await inbox.processFile(join(dir, 'never-existed.md'), null)

  expect(deliveries).toEqual([])
})
