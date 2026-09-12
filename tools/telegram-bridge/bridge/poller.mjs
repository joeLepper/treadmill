// poller.mjs — the sole getUpdates long-poller (ADR-0106). Exactly one of these
// runs against the shared bot token fleet-wide, so Telegram's one-poller-per-
// token limit is never contended and the 409 flap ends.
//
// Each round: long-poll getUpdates(offset,timeout); for every update, resolve
// its chat_id to a session label (the routing table doubles as the allowlist —
// an unmapped chat is dropped, fail-closed), inject the text into that
// session's relay dir, and commit the ledger. A non-text or unmapped update is
// advanced past (offset moves) but not injected.
//
// All I/O is injected (fetchFn, inject, sleep, log) so `node --test` drives the
// loop with a scripted fetch and no network. The default fetchFn is the global
// fetch (node >= 18).

const TELEGRAM_API = 'https://api.telegram.org'

// One getUpdates round. Returns the number of updates injected. Throws on a
// transport or Bot-API error so the caller applies backoff.
export async function pollOnce({ token, longPollSeconds = 25, fetchFn, inject, resolveLabel, ledger, log }) {
  const url = `${TELEGRAM_API}/bot${token}/getUpdates?offset=${ledger.offset}&timeout=${longPollSeconds}`
  const res = await fetchFn(url, { signal: AbortSignal.timeout((longPollSeconds + 10) * 1000) })
  if (!res.ok) throw new Error(`getUpdates HTTP ${res.status}`)
  const body = await res.json()
  if (!body.ok) throw new Error(`getUpdates not ok: ${body.description ?? 'unknown'}`)

  let injected = 0
  for (const update of body.result ?? []) {
    const updateId = update.update_id
    if (!Number.isInteger(updateId)) continue
    if (ledger.has(updateId)) { ledger.advance(updateId); continue } // effectively-once

    const msg = update.message
    const text = msg?.text
    const chatId = msg?.chat?.id
    const label = chatId != null ? resolveLabel(chatId) : null

    if (!label || typeof text !== 'string' || text.length === 0) {
      // Unmapped chat, or a non-text update (edited_message, callback, etc.).
      // Advance past it so it is not re-fetched; do not deliver anywhere.
      if (chatId != null && !label) log(`drop: no route for chat_id=${chatId} (update ${updateId})`)
      ledger.advance(updateId)
      continue
    }

    const from = msg.from?.username
      ? `@${msg.from.username}`
      : [msg.from?.first_name, msg.from?.last_name].filter(Boolean).join(' ') || undefined
    inject({ label, text, from, chatId, updateId, date: msg.date })
    ledger.commit(updateId)
    injected++
    log(`inject: chat_id=${chatId} -> ${label} (update ${updateId})`)
  }
  return injected
}

// The supervised loop. Long-polls forever; on an error, backs off with capped
// exponential delay, then resumes. systemd restarts the process only if the
// loop itself throws out (it does not — it backs off in-loop). `stop()` ends it.
export function runPoller(opts) {
  const { minBackoffMs = 1000, maxBackoffMs = 60000, sleep = ms => new Promise(r => setTimeout(r, ms)), log = () => {} } = opts
  let backoff = minBackoffMs
  let stopped = false
  const run = async () => {
    while (!stopped) {
      try {
        await pollOnce({ ...opts, log })
        backoff = minBackoffMs // a clean round resets backoff
      } catch (err) {
        if (stopped) break
        log(`poll error: ${err.message}; backoff ${backoff}ms`)
        await sleep(backoff)
        backoff = Math.min(backoff * 2, maxBackoffMs)
      }
    }
  }
  const done = run()
  return { stop: () => { stopped = true }, done }
}

export const _internal = { TELEGRAM_API }
