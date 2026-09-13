// `node --test`: the landing page's build. Run by `bin/ci`, beside the build itself.

import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { afterEach, describe, it } from 'node:test'
import { fileURLToPath } from 'node:url'

import { API_BASE_ENV, LOCAL_API_ORIGIN, PLACEHOLDER, build, resolveApiOrigin } from './build.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const scratch = []

function tempDir() {
  const dir = mkdtempSync(join(tmpdir(), 'motet-site-'))
  scratch.push(dir)
  return dir
}

afterEach(() => {
  for (const dir of scratch.splice(0)) rmSync(dir, { recursive: true, force: true })
})

describe('resolveApiOrigin', () => {
  it('defaults to the local API off Cloudflare, and says it did', () => {
    assert.deepEqual(resolveApiOrigin({}), { origin: LOCAL_API_ORIGIN, defaulted: true })
  })

  it('refuses to build on Cloudflare Pages without the variable', () => {
    // A production form posting to localhost would look perfect and collect nothing.
    assert.throws(() => resolveApiOrigin({ CF_PAGES: '1' }), new RegExp(API_BASE_ENV))
    assert.throws(() => resolveApiOrigin({ CF_PAGES: '1', [API_BASE_ENV]: '  ' }), /unset/)
  })

  it('reduces a base URL to its origin', () => {
    const env = { CF_PAGES: '1', [API_BASE_ENV]: 'https://API.example.test/' }
    assert.equal(resolveApiOrigin(env).origin, 'https://api.example.test')
    assert.equal(
      resolveApiOrigin({ [API_BASE_ENV]: 'http://localhost:8123' }).origin,
      'http://localhost:8123',
    )
  })

  for (const bad of [
    'api.example.test',
    'https:/api.example.test',
    'ftp://api.example.test',
    'https://api.example.test/v1',
    'https://api.example.test/?x=1',
    'https://user:pw@api.example.test',
  ]) {
    it(`refuses ${bad}`, () => {
      assert.throws(() => resolveApiOrigin({ [API_BASE_ENV]: bad }))
    })
  }

  it('refuses plain http on Cloudflare Pages', () => {
    assert.throws(
      () => resolveApiOrigin({ CF_PAGES: '1', [API_BASE_ENV]: 'http://api.example.test' }),
      /https/,
    )
  })
})

describe('build', () => {
  it('fills the API origin into the form and the security policy', () => {
    const out = tempDir()
    build({
      src: join(here, 'src'),
      out,
      env: { CF_PAGES: '1', [API_BASE_ENV]: 'https://api.example.test' },
    })

    const html = readFileSync(join(out, 'index.html'), 'utf8')
    const forms = [...html.matchAll(/<form[^>]*action="([^"]+)"/g)].map((match) => match[1])
    assert.ok(forms.length >= 1)
    for (const action of forms) assert.equal(action, 'https://api.example.test/v1/waitlist')

    const headers = readFileSync(join(out, '_headers'), 'utf8')
    assert.match(headers, /connect-src https:\/\/api\.example\.test;/)
    assert.match(headers, /form-action https:\/\/api\.example\.test;/)
    assert.ok(!html.includes(PLACEHOLDER) && !headers.includes(PLACEHOLDER))
  })

  it('keeps every waitlist form a real form with the honeypot and a status line', () => {
    const out = tempDir()
    build({ src: join(here, 'src'), out, env: {} })
    const html = readFileSync(join(out, 'index.html'), 'utf8')
    const forms = html.split('<form').slice(1)
    assert.ok(forms.length >= 1)
    for (const form of forms) {
      assert.match(form, /method="post"/)
      assert.match(form, /name="email" type="email" required/)
      assert.match(form, /name="motet_hp"[^>]*tabindex="-1"/)
      assert.match(form, /data-waitlist-status/)
    }
    // The script-src and style-src 'self' policy holds only while nothing is inline.
    assert.doesNotMatch(html, /\sstyle="/)
    assert.doesNotMatch(html, /<script(?![^>]*\ssrc=)[^>]*>/)
  })

  it('fails on a local reference nothing provides', () => {
    const src = tempDir()
    writeFileSync(join(src, 'index.html'), '<link rel="stylesheet" href="/missing.css">')
    assert.throws(() => build({ src, out: tempDir(), env: {} }), /missing\.css/)
  })

  it('fails on a placeholder nothing fills', () => {
    const src = tempDir()
    mkdirSync(join(src, 'nested'))
    writeFileSync(join(src, 'nested', 'page.html'), '<p>%%SOMETHING_ELSE%%</p>')
    assert.throws(() => build({ src, out: tempDir(), env: {} }), /placeholder/)
  })
})
