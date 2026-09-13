// Build the getmotet.com landing page: copy src/ to dist/ with the API's origin filled in.
//
// Cloudflare Pages runs this (root directory `site`, build command `npm run build`, output
// `dist`), and `bin/ci` runs it too — the "like Zimmer docs" shape: the site's only source
// is this directory, Cloudflare builds it from the repository, and CI proves it builds.
// Nothing here or in CI holds a Cloudflare credential.
//
// **The API origin is the build's one input, and it comes from the environment**, for the
// reason `web/` reads MOTET_API_BASE_URL at container start: this repo is public and names
// no deployment's hostname. Pages sets the variable per environment, so a preview build
// can post to staging and a production build to production. It lands in two places — the
// form's `action` and the Content-Security-Policy in `_headers` — which is why it is
// substituted at build time rather than read by the page's script at run time.
//
// **On Cloudflare an unset variable fails the build.** A production page whose form posts
// to localhost would look perfect and collect nothing, and the first sign would be an empty
// admin table weeks later. Off Cloudflare (a laptop, `bin/ci`) it defaults to the API
// `bin/dev` runs, and says so.
//
// Standard library only, deliberately: the brand allows two webfonts and nothing else, so
// there is no framework to install and `npm ci` has nothing to do.

import { cpSync, existsSync, mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

export const API_BASE_ENV = 'MOTET_API_BASE_URL'
export const LOCAL_API_ORIGIN = 'http://localhost:8000'
export const PLACEHOLDER = '%%MOTET_API_ORIGIN%%'

/** Files whose text is substituted; everything else is copied byte for byte. */
const TEXT = /\.(html|css|js|txt|svg|xml|webmanifest)$|^_headers$/

/**
 * The origin a browser should send the waitlist form to.
 *
 * Reduced to `scheme://host[:port]` and refused when it is anything else, for the reason
 * `motet_api.config._origin` gives: a one-slash typo or a trailing path yields a value that
 * builds cleanly and matches nothing, and here that is a form that silently goes nowhere.
 */
export function resolveApiOrigin(env) {
  const raw = (env[API_BASE_ENV] ?? '').trim()
  if (!raw) {
    if (env.CF_PAGES) {
      throw new Error(
        `${API_BASE_ENV} is unset. Set it on the Cloudflare Pages project, for Production and ` +
          'for Preview, to the base URL of the Motet API the waitlist form should post to.',
      )
    }
    return { origin: LOCAL_API_ORIGIN, defaulted: true }
  }
  let url
  try {
    url = new URL(raw)
  } catch {
    throw new Error(`${API_BASE_ENV}=${JSON.stringify(raw)} is not a URL.`)
  }
  if (!['http:', 'https:'].includes(url.protocol) || !/^https?:\/\/[^/]/.test(raw)) {
    throw new Error(`${API_BASE_ENV}=${JSON.stringify(raw)} must be an http(s) URL with a host.`)
  }
  if (url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw new Error(
      `${API_BASE_ENV}=${JSON.stringify(raw)} must be an origin (scheme, host, port) with no ` +
        'path, query or credentials — the form appends /v1/waitlist itself.',
    )
  }
  if (env.CF_PAGES && url.protocol !== 'https:') {
    throw new Error(`${API_BASE_ENV}=${JSON.stringify(raw)} must be https on Cloudflare Pages.`)
  }
  return { origin: url.origin, defaulted: false }
}

function* walk(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) yield* walk(path)
    else yield path
  }
}

/**
 * Every local `href`/`src` in the built HTML must exist in the output. A landing page is
 * small enough that a broken stylesheet or script link is the whole page broken.
 */
function missingReferences(out) {
  const missing = []
  for (const file of walk(out)) {
    if (!file.endsWith('.html')) continue
    const html = readFileSync(file, 'utf8')
    for (const [, ref] of html.matchAll(/\s(?:href|src)="([^"#]+)"/g)) {
      if (/^[a-z]+:/i.test(ref) || ref.startsWith('//')) continue
      const target = ref.startsWith('/') ? join(out, ref) : join(dirname(file), ref)
      const resolved = target.endsWith('/') ? join(target, 'index.html') : target
      if (!existsSync(resolved)) missing.push(`${relative(out, file)} → ${ref}`)
    }
  }
  return missing
}

export function build({ src, out, env }) {
  const { origin, defaulted } = resolveApiOrigin(env)
  rmSync(out, { recursive: true, force: true })
  mkdirSync(out, { recursive: true })
  cpSync(src, out, { recursive: true })

  for (const file of walk(out)) {
    const name = file.split(/[\\/]/).pop()
    if (!TEXT.test(name)) continue
    const text = readFileSync(file, 'utf8')
    if (text.includes(PLACEHOLDER)) writeFileSync(file, text.replaceAll(PLACEHOLDER, origin))
    if (/%%[A-Z_]+%%/.test(readFileSync(file, 'utf8'))) {
      throw new Error(`${relative(out, file)} still has a placeholder nothing fills.`)
    }
  }

  const missing = missingReferences(out)
  if (missing.length) {
    throw new Error(`Broken local references:\n  ${missing.join('\n  ')}`)
  }
  return { origin, defaulted }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const here = dirname(fileURLToPath(import.meta.url))
  try {
    const { origin, defaulted } = build({
      src: join(here, 'src'),
      out: join(here, 'dist'),
      env: process.env,
    })
    const note = defaulted ? ` (${API_BASE_ENV} unset; the local default)` : ''
    process.stdout.write(`site: built dist/, waitlist posts to ${origin}/v1/waitlist${note}\n`)
  } catch (error) {
    process.stderr.write(`site: build failed: ${error.message}\n`)
    process.exit(1)
  }
}
