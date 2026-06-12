/**
 * relay-inbox — transactional relay-file delivery (task ecd6d6eb).
 *
 * The 2026-06-12 drain-without-delivery race: on a --resume launch the
 * channel server drained the relay inbox at the FILESYSTEM level (zero
 * files left) but the resumed session never saw the content. Two
 * compounding defects in the old `processRelayFile`:
 *
 *   1. **unlink-before-notify** — the file was deleted, THEN the
 *      notification was sent. Any failed/landed-nowhere notification
 *      lost the message irrecoverably. (The old comment even named the
 *      pattern: "idempotent only by way of unlink-before-notify".)
 *   2. **drain-before-attach** — the startup drain ran immediately
 *      after `mcp.connect()`, but stdio connect only opens the
 *      transport; the CLIENT signals readiness later via the MCP
 *      `initialized` handshake. Notifications written into the resume
 *      window land nowhere.
 *
 * The fix is the #310 digest lesson one layer up — peek → deliver →
 * commit:
 *
 *   * a relay file is unlinked ONLY AFTER its notification resolves
 *     (notify-then-unlink); a failed send retains the file on disk;
 *   * the initial drain is GATED on the session being attached (the
 *     caller wires `markAttached()` to the MCP `initialized` signal);
 *     files watched/queued before attach simply wait;
 *   * a periodic RESWEEP retries anything retained by a failed send
 *     (fs.watch never re-fires for an existing file, so without the
 *     sweep a retained file would wait for the next restart);
 *   * an in-flight set replaces the old unlink-first idempotency:
 *     fs.watch fires multiple events per file, and notify-then-unlink
 *     would otherwise double-deliver.
 *
 * Delivery semantics move from at-most-once to AT-LEAST-ONCE: a crash
 * between notify and unlink redelivers on the next start. For relay
 * briefs that is the correct trade — duplicates are visible and
 * idempotent (senders tag task ids); silent loss cost ~3h on the live
 * incident.
 *
 * Pure logic + injected I/O seams (notify, clock) so `bun test` drives
 * it with real files and a scripted notifier; the fs.watch wiring stays
 * in treadmill-events.ts.
 */

import { readFile, readdir, unlink } from 'node:fs/promises'
import { join } from 'node:path'

export type RelayNotifyFn = (
  content: string,
  meta: Record<string, string>,
) => Promise<void>

export interface RelayInboxOpts {
  /** Subfolders (relative to the base dir) also swept on drain. */
  subfolders?: readonly string[]
  /** Injected for tests. */
  log?: (msg: string) => void
}

export class RelayInbox {
  private readonly dir: string
  private readonly notify: RelayNotifyFn
  private readonly subfolders: readonly string[]
  private readonly log: (msg: string) => void
  private attached = false
  private inFlight = new Set<string>()

  constructor(dir: string, notify: RelayNotifyFn, opts: RelayInboxOpts = {}) {
    this.dir = dir
    this.notify = notify
    this.subfolders = opts.subfolders ?? []
    this.log = opts.log ?? ((m: string) => console.error(m))
  }

  /** Whether the session surface is ready to receive notifications. */
  get isAttached(): boolean {
    return this.attached
  }

  /**
   * The session surface is provably ready (MCP `initialized` received).
   * Triggers the gated startup drain. Idempotent.
   */
  async markAttached(): Promise<void> {
    if (this.attached) return
    this.attached = true
    await this.drain()
  }

  /**
   * Sweep base dir + subfolders for pending `.md` relay files and
   * deliver each. No-op until attached — files persist on disk, which
   * IS the holding area (the durable leg of cc-relay).
   */
  async drain(): Promise<void> {
    if (!this.attached) return
    await this.drainDir(this.dir, null)
    for (const sub of this.subfolders) {
      await this.drainDir(join(this.dir, sub), sub)
    }
  }

  private async drainDir(dir: string, sub: string | null): Promise<void> {
    let names: string[]
    try {
      names = (await readdir(dir)).filter(f => f.endsWith('.md'))
    } catch {
      return // missing/unreadable dir — the watcher setup logs that case
    }
    for (const name of names) {
      await this.processFile(join(dir, name), sub)
    }
  }

  /**
   * Deliver one relay file transactionally: read → notify → unlink.
   *
   * - Not attached yet → leave the file untouched (the gated drain
   *   picks it up on attach).
   * - Notification failure → file RETAINED on disk; the resweep (or
   *   the next restart's drain) retries. This is the fix for the
   *   drain-without-delivery race: the unlink commits the delivery,
   *   never precedes it.
   * - In-flight set absorbs fs.watch's duplicate events per file (the
   *   old code got that for free by unlinking first — at the cost of
   *   losing the message whenever the notify never landed).
   */
  async processFile(fpath: string, sub: string | null): Promise<void> {
    if (!this.attached) return
    if (this.inFlight.has(fpath)) return
    this.inFlight.add(fpath)
    try {
      let content: string
      try {
        content = await readFile(fpath, 'utf-8')
      } catch {
        return // already consumed by a competing drain, or unreadable
      }
      const meta: Record<string, string> = { source: 'relay' }
      if (sub) meta.subfolder = sub
      try {
        await this.notify(content, meta)
      } catch (err) {
        this.log(
          `treadmill-events: relay notify failed for ${fpath}; ` +
            `file RETAINED for resweep/restart: ${err}`,
        )
        return
      }
      // Delivery resolved — commit by unlinking. A failure HERE means
      // at-least-once redelivery later, never loss.
      try {
        await unlink(fpath)
      } catch (err) {
        this.log(
          `treadmill-events: relay unlink failed for ${fpath} ` +
            `(delivered; may redeliver later): ${err}`,
        )
      }
    } finally {
      this.inFlight.delete(fpath)
    }
  }
}
