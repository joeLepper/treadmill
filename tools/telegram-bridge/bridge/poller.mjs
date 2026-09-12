// poller.mjs — the multi-token Telegram poller (ADR-0106). ONE daemon owns all N
// per-session bots; it runs one getUpdates loop per token, each bound to a fixed
// session label (the bot identity IS the routing key — each bot belongs to one
// session). Being the sole poller of each token ends the per-session MCP
// fragility; the tokens are read from the existing per-session telegram.env.
//
// Delivery is AT-LEAST-ONCE (see ADR-0106 / inject.mjs): inject publishes, then
// we ack via the ledger, then the channel server delivers-then-unlinks. A crash
// in any window redelivers; the update_id dedup only reduces duplicates.
//
// All I/O is injected (fetchFn, inject, ledger, quarantine, sleep, log) so
// `node --test` drives every branch with a scripted fetch and no network.

const TELEGRAM_API = 'https://api.telegram.org'

// A Bot-API 409 means another poller holds this token's slot — with the daemon
// live, that means a per-session MCP poller was not disabled. Surface it, don't
// silently retry forever.
export class ConflictError extends Error {}

// One getUpdates round for ONE bot. The target label is fixed. Returns
// { injected, dropped, quarantined }. Throws only on a transport / Bot-API
// error (so the caller backs off) — never on a single bad update.
export async function pollOnce({
  token, label, allowedChats, longPollSeconds = 25,
  fetchFn, inject, ledger,
  // Default THROWS, not a no-op: a caller that supplies no durable quarantine
  // must not silently ack (advance past) a failed inject and lose it.
  quarantine = () => { throw new Error('no quarantine handler configured — refusing to drop a failed inject') },
  log = () => {},
}) {
  const url = `${TELEGRAM_API}/bot${token}/getUpdates?offset=${ledger.offset}&timeout=${longPollSeconds}`
  const res = await fetchFn(url, { signal: AbortSignal.timeout((longPollSeconds + 10) * 1000) })
  // Classify a 409 by HTTP STATUS first — Telegram returns the "terminated by
  // other getUpdates request" conflict as HTTP 409, so this must precede the
  // generic !res.ok throw or a real conflict never reaches ConflictError.
  if (res.status === 409) throw new ConflictError(`409 for ${label} (another poller holds the slot)`)
  if (!res.ok) throw new Error(`getUpdates HTTP ${res.status} for ${label}`)
  const body = await res.json()
  if (!body.ok) {
    if (body.error_code === 409) throw new ConflictError(`409 for ${label} (another poller holds the slot)`)
    throw new Error(`getUpdates not ok for ${label}: ${body.description ?? 'unknown'}`)
  }

  let injected = 0, dropped = 0, quarantined = 0
  for (const update of body.result ?? []) {
    const updateId = update.update_id
    if (!Number.isInteger(updateId)) continue
    if (ledger.has(updateId)) { ledger.advance(updateId); continue }

    const msg = update.message
    const text = msg?.text
    const chatId = msg?.chat?.id

    // Mandatory per-bot sender allowlist (bypassed-permission sessions: ungated
    // inbound = code execution). A disallowed chat or non-text update is dropped
    // — advanced past, delivered nowhere.
    if (chatId == null || !allowedChats.has(String(chatId)) || typeof text !== 'string' || text.length === 0) {
      if (chatId != null && !allowedChats.has(String(chatId))) {
        log(`drop: chat_id=${chatId} not in allowlist for ${label} (update ${updateId})`)
      }
      ledger.advance(updateId); dropped++; continue
    }

    const from = msg.from?.username
      ? `@${msg.from.username}`
      : [msg.from?.first_name, msg.from?.last_name].filter(Boolean).join(' ') || undefined

    // Ordering matters for no-loss (Fran findings):
    //  - A failed inject is QUARANTINED before the offset advances. Quarantine
    //    must be DURABLE and must SUCCEED; if it throws, we do NOT advance —
    //    the throw propagates, the bot backs off, and the batch retries from
    //    the un-advanced offset. Never count a thrown quarantine as handled.
    //  - Once a message is delivered (inject returned), a later ledger-write
    //    failure must NOT quarantine an already-delivered message; it
    //    propagates so the batch retries (a duplicate on retry is fine —
    //    at-least-once), and the offset stays until the ack persists.
    let delivered = false
    try {
      inject({ label, text, from, chatId, updateId, date: msg.date })
      delivered = true
      ledger.commit(updateId); injected++
      log(`inject: ${label} (update ${updateId})`)
    } catch (err) {
      if (delivered) throw err // ledger write failed AFTER delivery — retain offset, retry; do not quarantine
      quarantine(update, err)  // durable; THROWS on failure → propagates, offset retained
      ledger.advance(updateId); quarantined++
      log(`quarantine: ${label} update ${updateId} inject failed: ${err.message}`)
    }
  }
  return { injected, dropped, quarantined }
}

// Supervised loop for ONE bot. Long-polls forever; on error, capped exponential
// backoff, then resumes. A persistent 409 is logged as contention on each retry.
export function runBot(opts) {
  const { label, minBackoffMs = 1000, maxBackoffMs = 60000,
    sleep = ms => new Promise(r => setTimeout(r, ms)), log = () => {} } = opts
  let backoff = minBackoffMs
  let stopped = false
  const run = async () => {
    while (!stopped) {
      try {
        await pollOnce({ ...opts, log })
        backoff = minBackoffMs
      } catch (err) {
        if (stopped) break
        if (err instanceof ConflictError) {
          log(`CONTENTION: ${err.message} — another poller holds ${label}'s slot (a per-session MCP not disabled?)`)
        } else {
          log(`poll error (${label}): ${err.message}; backoff ${backoff}ms`)
        }
        await sleep(backoff)
        backoff = Math.min(backoff * 2, maxBackoffMs)
      }
    }
  }
  return { stop: () => { stopped = true }, done: run() }
}

// The daemon: one runBot per configured bot, supervised independently, so one
// bot's failure never stalls another. `bots` is an array of per-bot opts
// (token, label, allowedChats, ledger, ...). Shared opts (fetchFn, inject,
// quarantine, sleep, log, backoff) are merged into each.
export function runDaemon({ bots, ...shared }) {
  const handles = bots.map(bot => runBot({ ...shared, ...bot }))
  return {
    stop: () => handles.forEach(h => h.stop()),
    done: Promise.all(handles.map(h => h.done)),
  }
}

export const _internal = { TELEGRAM_API }
