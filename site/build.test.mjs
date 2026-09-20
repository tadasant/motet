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

/**
 * The built `_headers` policy, as directive -> sources, so a test can assert the whole of it.
 *
 * Parsed as Cloudflare parses the file — a rule is an unindented path, its headers are the
 * indented lines under it — rather than by finding the first line that looks like a policy.
 * Where the policy *sits* is as load-bearing as what it says: a policy under `/index.html`,
 * or above the first rule, leaves `/` with no policy at all, and a second `/*` block could
 * add back everything the assertions below forbid. Both read identically to a line grep.
 */
function contentSecurityPolicy(headers) {
  const rules = new Map()
  let path = null
  for (const row of headers.split('\n')) {
    if (!row.trim() || row.trimStart().startsWith('#')) continue
    if (!/^\s/.test(row)) {
      path = row.trim()
      rules.set(path, [])
      continue
    }
    assert.ok(path, '_headers has a header line before any path rule')
    rules.get(path).push(row.trim())
  }
  assert.deepEqual([...rules.keys()], ['/*'], '_headers must apply to /* and nothing else')

  const lines = rules.get('/*').filter((row) => row.startsWith('Content-Security-Policy:'))
  assert.equal(lines.length, 1, '/* must set exactly one Content-Security-Policy')
  const policy = {}
  for (const directive of lines[0].slice('Content-Security-Policy:'.length).split(';')) {
    const [name, ...sources] = directive.trim().split(/\s+/)
    if (name) policy[name] = sources
  }
  return policy
}

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
    const policy = contentSecurityPolicy(headers)
    assert.deepEqual(policy['connect-src'], ["'self'", 'https://api.example.test'])
    assert.deepEqual(policy['form-action'], ['https://api.example.test'])
    assert.ok(!html.includes(PLACEHOLDER) && !headers.includes(PLACEHOLDER))
  })

  it('lets Cloudflare’s analytics beacon load and report, and nothing else', () => {
    // Cloudflare injects the beacon into every proxied HTML response on this zone, so the
    // page's own markup never mentions it and only this policy decides whether it runs.
    // Observed against the live site: the script comes from static.cloudflareinsights.com
    // under a changing build suffix, and the automatic injection reports to this origin's
    // own /cdn-cgi/rum. See the comment at the top of src/_headers.
    const out = tempDir()
    build({ src: join(here, 'src'), out, env: {} })
    const policy = contentSecurityPolicy(readFileSync(join(out, '_headers'), 'utf8'))

    assert.deepEqual(policy['script-src'], ["'self'", 'https://static.cloudflareinsights.com'])
    assert.ok(policy['connect-src'].includes("'self'"))

    // Automatic injection posts same-origin, so the reporting host is deliberately absent.
    // The `deepEqual` above is what pins the script source, path forms included.
    assert.ok(!policy['connect-src'].includes('https://cloudflareinsights.com'))
  })

  it('keeps the policy free of wildcards and inline escapes', () => {
    const out = tempDir()
    build({ src: join(here, 'src'), out, env: {} })
    const policy = contentSecurityPolicy(readFileSync(join(out, '_headers'), 'utf8'))

    assert.deepEqual(policy['default-src'], ["'self'"])
    assert.deepEqual(policy['object-src'], ["'none'"])
    assert.deepEqual(policy['frame-ancestors'], ["'none'"])
    assert.deepEqual(policy['base-uri'], ["'self'"])
    // The three directives with no placeholder in them are pinned outright, so a host
    // added to any of them is a failure rather than merely a well-shaped source.
    assert.deepEqual(policy['style-src'], ["'self'", 'https://fonts.googleapis.com'])
    assert.deepEqual(policy['font-src'], ['https://fonts.gstatic.com'])
    assert.deepEqual(policy['img-src'], ["'self'", 'data:'])

    // And a sweep over everything, including the two directives the build substitutes
    // into: every source is a keyword or one exact host. No `*`, no bare scheme, no
    // inline escape — a beacon is not a reason to reach for any of them.
    const keyword = /^'(self|none)'$/
    const exactHost = /^https:\/\/[a-z0-9.-]+$/
    for (const [directive, sources] of Object.entries(policy)) {
      for (const source of sources) {
        const allowed =
          keyword.test(source) ||
          exactHost.test(source) ||
          (directive === 'img-src' && source === 'data:') ||
          source === LOCAL_API_ORIGIN
        assert.ok(allowed, `${directive} must not widen to ${source}`)
      }
    }
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
