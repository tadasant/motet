// The callback path, and the two things that have to survive a round trip to Google.
//
// **This is the whole of the SPA's routing**, and it is deliberately not a router.
// App.tsx says "three screens, one tab strip, no router" — adding a routing library to
// serve one path that the user is on for about two seconds would be the first step
// toward building a product instead of a factory (a named tripwire in AGENTS.md). So the
// path is read once at boot, and the rest of the app never thinks about the URL again.
//
// web/nginx.conf's history fallback (`try_files $uri $uri/ /index.html`) is what makes
// that work in the deployed image: /oauth/callback is not a file, so nginx serves the
// bundle and the bundle reads the path.

const CALLBACK_PATH = '/oauth/callback'

/**
 * Where Google sends the user back to.
 *
 * Derived from the origin rather than configured, which is what makes one bundle serve
 * every environment — the same property `config.js` buys for the API origin. Every
 * environment's URI is therefore its own origin plus this path, and the *path* is the
 * part that must not drift: it is registered on the OAuth client, and the registrations
 * live in the private infrastructure repo, so changing it here silently breaks consent
 * everywhere it is deployed and nothing in this repo would notice.
 *
 * Google matches a redirect URI by exact string, which in dev means reaching the Vite
 * server at `localhost:5173` and not `127.0.0.1:5173`: same server, different string,
 * and the mismatch surfaces as `redirect_uri_mismatch` on Google's own error page rather
 * than anywhere in this app. The connect form prints the value it is about to send for
 * exactly that reason.
 */
export function redirectUri(): string {
  return `${window.location.origin}${CALLBACK_PATH}`
}

/**
 * Hand the browser to the provider's consent screen.
 *
 * A named function rather than an inline `window.location.assign`, because it is the one
 * line in the connect flow a test cannot execute — jsdom has no navigation — so the
 * screen takes it as a prop defaulting to this.
 */
export function beginConsent(url: string): void {
  window.location.assign(url)
}

/** What Google put in the query string when it sent the user back. */
export type OAuthCallback =
  | { kind: 'granted'; code: string; state: string }
  /** The user said no, or Google refused. `error` is its own code, e.g. access_denied. */
  | { kind: 'denied'; error: string; description: string }
  /** On the callback path with nothing usable — a bookmark, or a reload after finishing. */
  | { kind: 'empty' }

/**
 * Read the callback out of the URL, once, at boot. `null` means "this is a normal load".
 *
 * Pure: it inspects `location` and changes nothing, so it is safe in a `useState`
 * initializer, which React may call more than once. Clearing the URL is
 * `forgetCallbackUrl` below, and it is a separate step on purpose.
 */
export function readCallback(location: Location = window.location): OAuthCallback | null {
  if (location.pathname.replace(/\/+$/, '') !== CALLBACK_PATH) return null

  const params = new URLSearchParams(location.search)
  const error = params.get('error')
  if (error) {
    return { kind: 'denied', error, description: params.get('error_description') ?? '' }
  }

  const code = params.get('code')
  const state = params.get('state')
  if (code && state) return { kind: 'granted', code, state }

  return { kind: 'empty' }
}

/**
 * Put the address bar back to `/`.
 *
 * An authorization code is single-use, so leaving it in the URL does not let anyone
 * replay it — but it does leave it in browser history and in whatever syncs that, and a
 * reload would re-POST a code the API has already consumed and answer the user with
 * "already used" on a flow that in fact succeeded.
 */
export function forgetCallbackUrl(): void {
  window.history.replaceState({}, '', '/')
}

const STATE_STORAGE_KEY = 'motet.oauthState'

/**
 * The `state` the API minted for this authorization, kept across the redirect.
 *
 * sessionStorage rather than localStorage: this is per-tab and worthless a minute later,
 * and a stale value in a second tab is exactly the confusion it exists to prevent. The
 * API is the real check — it consumes the row with a `DELETE ... RETURNING`, so an
 * unknown or replayed state is rejected there whatever the client believes. This is the
 * cheap half: it catches a callback that belongs to a different authorization before
 * spending a round trip on it.
 */
export function rememberState(state: string): void {
  try {
    window.sessionStorage.setItem(STATE_STORAGE_KEY, state)
  } catch {
    // Private browsing and some embedded webviews throw. Losing the check is survivable;
    // `takeState` returns '' and the API decides instead.
  }
}

/** Read the remembered state and forget it — it is good for one callback. */
export function takeState(): string {
  try {
    const state = window.sessionStorage.getItem(STATE_STORAGE_KEY) ?? ''
    window.sessionStorage.removeItem(STATE_STORAGE_KEY)
    return state
  } catch {
    return ''
  }
}

/**
 * Whether a callback's state matches the one we started with.
 *
 * An *absent* remembered state passes. The tab may have been restored, the store may
 * have thrown, or consent may have finished in a different tab — none of which is
 * evidence of an attack, and all of which the API will judge properly a moment later.
 * A *mismatched* one fails, because that is a callback for an authorization this tab did
 * not begin.
 */
export function stateMatches(expected: string, received: string): boolean {
  return expected === '' || expected === received
}
