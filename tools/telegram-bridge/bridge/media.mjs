// media.mjs — download an inbound Telegram photo/document so the receiving
// session can open it. The relay message then carries the local path instead of
// a "text only" stub.
//
// The bot token is part of every Bot API URL, so no error message here may
// include a URL: callers log these messages.

import { closeSync, fsyncSync, openSync, renameSync, unlinkSync, writeFileSync } from 'node:fs'
import { basename, join } from 'node:path'
import { randomUUID } from 'node:crypto'
import { mkdirpDurable } from './durable.mjs'

const TELEGRAM_API = 'https://api.telegram.org'
// Bot API getFile refuses files above 20 MB.
export const MAX_MEDIA_BYTES = 20 * 1024 * 1024

// The downloadable part of a message, or null. A photo arrives as several
// sizes; the last entry is the largest.
export function mediaRef(m) {
  if (Array.isArray(m.photo) && m.photo.length > 0) {
    const p = m.photo[m.photo.length - 1]
    return { kind: 'photo', fileId: p.file_id, fileSize: p.file_size, fileName: null }
  }
  if (m.document?.file_id) {
    return { kind: 'document', fileId: m.document.file_id, fileSize: m.document.file_size, fileName: m.document.file_name ?? null }
  }
  return null
}

function safeName(name) {
  const b = basename(String(name ?? '')).replace(/[^A-Za-z0-9._-]/g, '_').replace(/^\.+/, '')
  return b.slice(-100) || 'file'
}

// Returns async ({ kind, fileId, fileSize, fileName, updateId }) => absolute path.
// The poll loop waits on this, so the timeout bounds how long a media message
// can delay the rest of its batch.
export function makeMediaFetcher({ token, dir, anchor, fetchFn, maxBytes = MAX_MEDIA_BYTES, timeoutMs = 30000 }) {
  const download = downloader({ token, dir, anchor, fetchFn, maxBytes, timeoutMs })
  return async ref => {
    try {
      return await download(ref)
    } catch (err) {
      // fetch can echo the URL (e.g. "Failed to parse URL from …"), and the
      // caller logs and injects this message.
      const msg = String(err?.message ?? err)
      throw new Error(token ? msg.split(token).join('<token>') : msg)
    }
  }
}

function downloader({ token, dir, anchor, fetchFn, maxBytes, timeoutMs }) {
  return async ({ fileId, fileSize, fileName, updateId }) => {
    if (fileSize != null && fileSize > maxBytes) throw new Error(`file too large (${fileSize} bytes)`)
    const meta = await fetchFn(`${TELEGRAM_API}/bot${token}/getFile?file_id=${encodeURIComponent(fileId)}`,
      { signal: AbortSignal.timeout(timeoutMs) })
    if (!meta.ok) throw new Error(`getFile HTTP ${meta.status}`)
    const body = await meta.json()
    const filePath = body?.result?.file_path
    if (!body?.ok || typeof filePath !== 'string') throw new Error(`getFile not ok: ${body?.description ?? 'no file_path'}`)
    const res = await fetchFn(`${TELEGRAM_API}/file/bot${token}/${filePath}`, { signal: AbortSignal.timeout(timeoutMs) })
    if (!res.ok) throw new Error(`file download HTTP ${res.status}`)
    const declared = Number(res.headers?.get?.('content-length'))
    if (declared > maxBytes) throw new Error(`file too large (${declared} bytes)`)
    const buf = Buffer.from(await res.arrayBuffer())
    if (buf.length > maxBytes) throw new Error(`file too large (${buf.length} bytes)`)

    mkdirpDurable(dir, anchor)
    const finalPath = join(dir, `tg-${updateId}-${safeName(fileName ?? filePath)}`)
    const tmp = `${finalPath}.${randomUUID()}.tmp`
    let fd
    try {
      fd = openSync(tmp, 'wx', 0o600)
      writeFileSync(fd, buf)
      fsyncSync(fd)
      closeSync(fd); fd = undefined
      renameSync(tmp, finalPath)
    } finally {
      if (fd !== undefined) closeSync(fd)
      try { unlinkSync(tmp) } catch (e) { if (e.code !== 'ENOENT') throw e }
    }
    return finalPath
  }
}

export const _internal = { safeName }
