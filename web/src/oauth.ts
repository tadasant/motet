// The callback path, and the three things that have to survive a round trip to Google.
//
// **It is read once at boot, and apart from the shell.** The shell keeps the section in
// the path (`shell/useLocation.ts`), but that is one string in React state, not a router,
// and it never sees this path: App.tsx renders the callback instead of the shell, and
// finishing hands over to a section with `replace`, so Back does not return to a spent
// code. Keeping the two apart is what lets the section URLs change without touching the
// one path that is registered on the OAuth client.
//
// web/nginx.conf's history fallback (`try_files $uri $uri/ /index.html`) is what makes
// that work in the deployed image: /oauth/callback is not a file, so nginx serves the
// bundle and the bundle reads the path. Every section path works the same way.

const CALLBACK_PATH = '/oauth/callback'

/**
 * How a callback says which flow it belongs to.
 *
 * **Three flows land on this one path**: signing in, connecting a mailbox, and authorizing
 * an MCP server (below). They finish
 * at different API routes and spend a single-use `state` doing it, so sending one to the
 * other's route burns the authorization and the user has to start again for no visible
 * reason.
 *
 * `state` is the discriminator because it is the only value guaranteed to survive the
 * round trip — Google echoes it back verbatim, and the browser arrives here with a fresh
 * page load and no memory of anything else. The API mints sign-in states with this
 * prefix; the dot is safe as a marker because `secrets.token_urlsafe` emits only
 * `[A-Za-z0-9_-]`, so a mailbox state can never accidentally look like a sign-in one.
 *
 * Keep in step with `LOGIN_STATE_PREFIX` in `motet_api.auth.registry`.
 */
const LOGIN_STATE_PREFIX = 'login.'

/** Whether a callback's `state` belongs to a sign-in rather than to a mailbox. */
export function isLoginState(state: string): boolean {
  return state.startsWith(LOGIN_STATE_PREFIX)
}

/**
 * The third flow on this path: authorizing an MCP server from the Credentials screen
 * (motet#102). Same discriminator, same reason. Keep in step with `CONNECTOR_STATE_PREFIX`
 * in `motet_api.connectors`.
 */
const CONNECTOR_STATE_PREFIX = 'connector.'

/** Whether a callback's `state` belongs to a connector's authorization. */
export function isConnectorState(state: string): boolean {
  return state.startsWith(CONNECTOR_STATE_PREFIX)
}

/**
 * The fourth flow on this path: an MCP client's authorization (motet#111).
 *
 * **This tab did not start it.** The client sent the person to the API's `/authorize`,
 * which sent them to Google, so there is no remembered state in sessionStorage to compare
 * with — the API's single-use state row is the only check, and the screen does not call
 * `takeState` for it. Same dot, same reason as the sign-in prefix above.
 *
 * Keep in step with `MCP_STATE_PREFIX` in `motet_api.mcp.oauth`.
 */
const MCP_STATE_PREFIX = 'mcp.'

/** Whether a callback's `state` belongs to an MCP client's authorization. */
export function isMcpState(state: string): boolean {
  return state.startsWith(MCP_STATE_PREFIX)
}

/** Schemes that run or render content in this origin's place, rather than hand off to a client. */
const REFUSED_REDIRECT_SCHEMES = new Set(['javascript:', 'data:', 'vbscript:', 'file:', 'blob:', 'about:'])

/**
 * Whether the SPA may send the browser to an MCP client's redirect URI (motet#111).
 *
 * **The navigation is the grant, so the target is checked before either button exists.**
 * Registration is unauthenticated, so the URI is whatever a stranger registered, and
 * `location.assign('javascript:…')` would run that stranger's script on this origin, where
 * the session token lives, whichever button was pressed. The API refuses such a client at
 * registration and again when it mints the code; this is the third check, on the origin it
 * protects. `https` anywhere, `http` only on a loopback address (RFC 8252's native-app
 * redirect), and a private-use scheme such as `vscode:` so a desktop client can be handed
 * back to.
 *
 * Keep in step with `redirect_uri_allowed` in `motet_api.mcp.oauth`.
 */
export function isSafeClientRedirect(url: string): boolean {
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    return false
  }
  const scheme = parsed.protocol.toLowerCase()
  if (REFUSED_REDIRECT_SCHEMES.has(scheme)) return false
  if (scheme === 'https:') return true
  if (scheme === 'http:') return ['localhost', '127.0.0.1', '[::1]'].includes(parsed.hostname)
  return /^[a-z][a-z0-9+.-]*:$/.test(scheme)
}

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
  /**
   * `iss` is RFC 9207's issuer identifier. An MCP authorization server that supports it
   * sends one and Google does not; it is carried so the connector callback can refuse a
   * code that arrived under a different issuer than discovery found.
   */
  | { kind: 'granted'; code: string; state: string; iss?: string }
  /**
   * The user said no, or Google refused. `error` is its own code, e.g. access_denied.
   *
   * `state` is carried even though nothing is exchanged, because it is still what says
   * which flow was refused — "you did not grant access to your mailbox" and "you did not
   * finish signing in" are different sentences.
   */
  | { kind: 'denied'; error: string; description: string; state: string }
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
    return {
      kind: 'denied',
      error,
      description: params.get('error_description') ?? '',
      state: params.get('state') ?? '',
    }
  }

  const code = params.get('code')
  const state = params.get('state')
  const iss = params.get('iss')
  if (code && state) return iss ? { kind: 'granted', code, state, iss } : { kind: 'granted', code, state }

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
