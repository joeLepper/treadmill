import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, statSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { makeMediaFetcher, mediaRef, _internal } from './media.mjs'

const TOKEN = '123:SECRET-TOKEN'

function scriptedFetch({ meta = { ok: true, result: { file_path: 'photos/file_7.jpg' } }, metaStatus = 200, fileStatus = 200, bytes = Buffer.from('JPEGDATA') } = {}) {
  const urls = []
  const fetchFn = async url => {
    urls.push(url)
    if (url.includes('/getFile?')) return { ok: metaStatus === 200, status: metaStatus, json: async () => meta }
    return { ok: fileStatus === 200, status: fileStatus, arrayBuffer: async () => bytes }
  }
  return { fetchFn, urls }
}

function setup(opts = {}) {
  const anchor = mkdtempSync(join(tmpdir(), 'tg-media-'))
  const dir = join(anchor, 'treadmill-carla', 'media')
  const s = scriptedFetch(opts)
  return { anchor, dir, ...s, fetchMedia: makeMediaFetcher({ token: TOKEN, dir, anchor, fetchFn: s.fetchFn, ...(opts.extra ?? {}) }) }
}

test('mediaRef picks the largest photo size, then documents, else null', () => {
  assert.equal(mediaRef({ photo: [{ file_id: 'a' }, { file_id: 'b', file_size: 9 }] }).fileId, 'b')
  assert.deepEqual(mediaRef({ document: { file_id: 'd', file_name: 'x.pdf' } }),
    { kind: 'document', fileId: 'd', fileSize: undefined, fileName: 'x.pdf' })
  assert.equal(mediaRef({ text: 'hi' }), null)
  assert.equal(mediaRef({ photo: [] }), null)
})

test('downloads the file into dir with mode 0600 and returns its path', async () => {
  const { dir, urls, fetchMedia } = setup()
  const p = await fetchMedia({ kind: 'photo', fileId: 'f/1', updateId: 7, fileName: null })
  assert.equal(p, join(dir, 'tg-7-file_7.jpg'))
  assert.equal(readFileSync(p, 'utf8'), 'JPEGDATA')
  assert.equal(statSync(p).mode & 0o777, 0o600)
  assert.match(urls[0], /getFile\?file_id=f%2F1$/)
  assert.match(urls[1], /\/file\/bot123:SECRET-TOKEN\/photos\/file_7\.jpg$/)
  assert.deepEqual(readdirSync(dir), ['tg-7-file_7.jpg'], 'no .tmp left behind')
})

test('a hostile file name cannot escape dir', async () => {
  const { dir, fetchMedia } = setup()
  const p = await fetchMedia({ kind: 'document', fileId: 'x', updateId: 8, fileName: '../../../etc/pass wd' })
  assert.equal(p, join(dir, 'tg-8-pass_wd'))
  assert.equal(_internal.safeName('..'), 'file')
})

test('refuses a file over the cap before and after download', async () => {
  const a = setup({ extra: { maxBytes: 4 } })
  await assert.rejects(a.fetchMedia({ fileId: 'x', updateId: 1, fileSize: 5 }), /too large/)
  assert.equal(a.urls.length, 0, 'the declared size is checked before any request')
  await assert.rejects(a.fetchMedia({ fileId: 'x', updateId: 2 }), /too large/)
})

test('a fetch error that echoes the URL is redacted', async () => {
  const anchor = mkdtempSync(join(tmpdir(), 'tg-media-'))
  const fetchFn = async url => { throw new TypeError(`Failed to parse URL from ${url}`) }
  const fetchMedia = makeMediaFetcher({ token: TOKEN, dir: join(anchor, 'm'), anchor, fetchFn })
  const err = await fetchMedia({ fileId: 'x', updateId: 4 }).then(() => null, e => e)
  assert.ok(err)
  assert.ok(!err.message.includes('SECRET'), `token leaked: ${err.message}`)
  assert.match(err.message, /bot<token>\/getFile/)
})

test('a declared content-length over the cap is refused before the body is read', async () => {
  const anchor = mkdtempSync(join(tmpdir(), 'tg-media-'))
  let bodyRead = false
  const fetchFn = async url => url.includes('/getFile?')
    ? { ok: true, status: 200, json: async () => ({ ok: true, result: { file_path: 'a.jpg' } }) }
    : { ok: true, status: 200, headers: new Headers({ 'content-length': '99' }), arrayBuffer: async () => { bodyRead = true; return Buffer.alloc(1) } }
  const fetchMedia = makeMediaFetcher({ token: TOKEN, dir: join(anchor, 'm'), anchor, fetchFn, maxBytes: 10 })
  await assert.rejects(fetchMedia({ fileId: 'x', updateId: 5 }), /too large \(99 bytes\)/)
  assert.equal(bodyRead, false)
})

test('errors never include the token', async () => {
  for (const opts of [{ metaStatus: 401 }, { meta: { ok: false, description: 'bad file_id' } }, { fileStatus: 404 }]) {
    const { fetchMedia } = setup(opts)
    const err = await fetchMedia({ fileId: 'x', updateId: 3 }).then(() => null, e => e)
    assert.ok(err, `expected a rejection for ${JSON.stringify(opts)}`)
    assert.ok(!err.message.includes('SECRET'), `token leaked: ${err.message}`)
  }
})
