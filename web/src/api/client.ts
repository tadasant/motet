// The only way the SPA reaches the outside world.
//
// Invariant 1: the client never speaks a vendor protocol. Everything goes through the
// Motet API, whose shape comes from `schema.gen.ts` — generated from `openapi.yaml`,
// which is itself generated from the FastAPI app. Never hand-write a request type here;
// change the API and regenerate, or the contract stops being one.

import type { paths } from './schema.gen'

/** Response body of a GET, typed straight off the generated contract. */
export type GetResponse<P extends keyof paths> = paths[P] extends {
  get: { responses: { 200: { content: { 'application/json': infer R } } } }
}
  ? R
  : never

/** Response body of a POST, whether the route answers 200 or 201. */
export type PostResponse<P extends keyof paths> = paths[P] extends {
  post: { responses: infer R }
}
  ? R extends { 201: { content: { 'application/json': infer C } } }
    ? C
    : R extends { 200: { content: { 'application/json': infer C } } }
      ? C
      : never
  : never

/** Response body of a DELETE that answers 200 with a body rather than 204. */
export type DeleteResponse<P extends keyof paths> = paths[P] extends {
  delete: { responses: { 200: { content: { 'application/json': infer R } } } }
}
  ? R
  : never

/** Response body of a PUT. */
export type PutResponse<P extends keyof paths> = paths[P] extends {
  put: { responses: { 200: { content: { 'application/json': infer R } } } }
}
  ? R
  : never

export type HealthResponse = GetResponse<'/internal/health'>
export type NewsItem = GetResponse<'/v1/news-items'>[number]
export type NewsItemDetail = GetResponse<'/v1/news-items/{news_item_id}'>
export type NewsItemSource = NewsItemDetail['sources'][number]
export type IngestionItem = GetResponse<'/v1/ingestion'>[number]
export type ProcessingStatus = GetResponse<'/v1/processing'>
export type Episode = GetResponse<'/v1/episodes'>[number]
export type EpisodeSegment = Episode['segments'][number]
export type Claim = EpisodeSegment['claims'][number]
export type FeedInfo = GetResponse<'/v1/feed'>
export type SourceItem = PostResponse<'/v1/sources/paste'>
export type Source = GetResponse<'/v1/sources'>[number]
export type LabelSync = NonNullable<Source['label_sync']>
export type Connection = PostResponse<'/v1/sources/connect'>
export type HeldSourceItem = GetResponse<'/v1/source-items/held'>[number]
export type Connector = GetResponse<'/v1/connectors'>[number]
export type ConnectorAuthorization = PostResponse<'/v1/connectors/{connector_id}/authorize'>
/** Request body of `POST /v1/connectors`. */
export type CreateConnector = NonNullable<
  paths['/v1/connectors']['post']['requestBody']
>['content']['application/json']
export type SourceItemDetail = GetResponse<'/v1/source-items/{source_item_id}'>
export type ProcessingStep = SourceItemDetail['processed'][number]
export type SignInStart = PostResponse<'/v1/auth/google/start'>
export type SignedIn = PostResponse<'/v1/auth/google/callback'>
export type McpAuthorization = PostResponse<'/v1/auth/mcp/callback'>
export type SessionInfo = GetResponse<'/v1/auth/session'>
export type ApiToken = GetResponse<'/v1/auth/tokens'>[number]
export type MintedApiToken = PostResponse<'/v1/auth/tokens'>
export type AdminOverview = GetResponse<'/v1/admin/overview'>
export type AdminWaitlist = GetResponse<'/v1/admin/waitlist'>
export type VoiceStatus = GetResponse<'/v1/voice'>
export type VoiceSession = PostResponse<'/v1/episodes/{episode_id}/voice-session'>
export type LlmConfig = GetResponse<'/v1/admin/llm-config'>
export type LlmStageConfig = LlmConfig['stages'][number]
export type LlmSpendReport = GetResponse<'/v1/admin/llm-spend'>
export type LlmSpend = LlmSpendReport['total']['stages'][string]
/** Request body of `PUT /v1/admin/llm-config/{stage}`: a key left out is untouched, null clears. */
export type LlmStageUpdate = NonNullable<
  paths['/v1/admin/llm-config/{stage}']['put']['requestBody']
>['content']['application/json']

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

/**
 * Runtime configuration, served by the container rather than compiled into the bundle.
 *
 * `config.js` is written by the web image's entrypoint from the environment Cloud Run
 * gives it, and loaded by index.html before the bundle. In dev it is the checked-in
 * placeholder under `web/public/`, which sets nothing.
 */
declare global {
  interface Window {
    __MOTET_CONFIG__?: { apiBaseUrl?: string }
  }
}

/**
 * Where the API lives. Runtime first, build time second, same-origin last.
 *
 * **Runtime first is the load-bearing part.** The SPA and the API are served from two
 * different hostnames in every deployed environment — `app.` and `api.` — so the bundle
 * cannot use same-origin paths, and it cannot bake the hostname in either: Vite inlines
 * `import.meta.env` at build time, so a compiled-in value would make one image per
 * environment and would silently ignore the `MOTET_API_BASE_URL` the service definition
 * already sets. One image, configured where it runs.
 *
 * `import.meta.env.VITE_API_BASE_URL` is kept as the second choice for `npm run dev`
 * against a non-default API. The empty fallback is what the dev server wants, because
 * vite.config.ts proxies `/v1` to the local API and same-origin is then correct.
 *
 * Real hostnames live in the private infrastructure repo, never in this tree.
 */
export function apiBaseUrl(): string {
  const runtime = globalThis.window?.__MOTET_CONFIG__?.apiBaseUrl
  // Both branches go through `normalise`, so `VITE_API_BASE_URL=http://127.0.0.1:8000/`
  // cannot produce `http://127.0.0.1:8000//v1/...` while the runtime path handles it.
  return normalise(runtime) || normalise(import.meta.env.VITE_API_BASE_URL)
}

/** Trim, drop trailing slashes, and treat anything blank or non-string as unset. */
function normalise(value: unknown): string {
  if (typeof value !== 'string') return ''
  return value.trim().replace(/\/+$/, '')
}

const TOKEN_STORAGE_KEY = 'motet.apiToken'

/**
 * The bearer token this browser presents, kept in localStorage.
 *
 * **One slot, two ways of filling it**, which is the whole shape of Google Sign-In here:
 * signing in mints a *session* token and puts it in this same place, so every request
 * below is made exactly as it was before and no call site knows the difference. The
 * shared `MOTET_API_TOKEN` still works and is still typed in by hand under the API-token
 * disclosure — the feed, the iOS app and any script use it — it just stops being the
 * thing a human has to paste into a phone.
 *
 * There is still exactly one account behind either. This is a lock on the door, not an
 * identity system; real accounts are Phase 3.
 */
export function getToken(): string {
  try {
    return window.localStorage.getItem(TOKEN_STORAGE_KEY) ?? ''
  } catch {
    // Private browsing and some embedded webviews throw on localStorage rather than
    // returning null. An unusable store is the same as an empty one.
    return ''
  }
}

export function setToken(token: string): void {
  try {
    if (token) {
      window.localStorage.setItem(TOKEN_STORAGE_KEY, token)
    } else {
      window.localStorage.removeItem(TOKEN_STORAGE_KEY)
    }
  } catch {
    // Nothing useful to do: the request below will fail with a 401 and say so.
  }
}

function headers(): Record<string, string> {
  const token = getToken()
  return {
    Accept: 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  }
}

/**
 * `fetch`, with the one failure it reports as nothing at all turned into a sentence.
 *
 * A rejected `fetch` means the request never completed at the network layer, and the
 * browser deliberately tells JavaScript nothing about why — the same opaque
 * `TypeError: Failed to fetch` covers a refused cross-origin response, a DNS failure, a
 * TLS error, an offline device and a server that closed the connection. That string
 * reached a user verbatim once already, as the only evidence that connecting Gmail was
 * broken, and it named neither the URL nor the fact that a request was even attempted.
 *
 * `status` is 0 because there was no response to have one — the same convention
 * `XMLHttpRequest` uses, and what tells a caller apart from an HTTP error it could act on.
 */
async function send(url: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(url, init)
  } catch (cause) {
    throw new ApiError(
      0,
      `Could not reach the API at ${url} — the request never completed. That is a ` +
        'network, DNS, TLS or cross-origin failure rather than an answer from Motet: ' +
        `${cause instanceof Error ? cause.message : String(cause)}`,
    )
  }
}

async function refuse(response: Response, method: string, path: string): Promise<ApiError> {
  // The API answers with `{"detail": "..."}`; a proxy or a crash might not. Falling back
  // to the status keeps an error message from being the literal string "undefined".
  let detail = `${response.status}`
  try {
    const body = (await response.json()) as { detail?: unknown }
    if (typeof body.detail === 'string') detail = body.detail
  } catch {
    detail = response.statusText || detail
  }
  return new ApiError(response.status, `${method} ${path} failed: ${detail}`)
}

async function parse<T>(response: Response, method: string, path: string): Promise<T> {
  if (!response.ok) throw await refuse(response, method, path)
  return (await response.json()) as T
}

export async function apiGet<P extends keyof paths>(path: P): Promise<GetResponse<P>> {
  const response = await send(`${apiBaseUrl()}${path}`, { headers: headers() })
  return parse<GetResponse<P>>(response, 'GET', path)
}

export async function apiPost<P extends keyof paths>(
  path: P,
  body?: unknown,
): Promise<PostResponse<P>> {
  const response = await send(`${apiBaseUrl()}${path}`, {
    method: 'POST',
    headers: { ...headers(), 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  })
  return parse<PostResponse<P>>(response, 'POST', path)
}

/**
 * A POST whose success is 204 and therefore has no body to parse.
 *
 * Its own function rather than a flag on `apiPost`, because `response.json()` on an empty
 * body throws — so "no content" has to be a different code path, not a different argument.
 */
export async function apiPostNoContent<P extends keyof paths>(path: P): Promise<void> {
  const response = await send(`${apiBaseUrl()}${path}`, {
    method: 'POST',
    headers: { ...headers(), 'Content-Type': 'application/json' },
    body: '{}',
  })
  if (!response.ok) throw await refuse(response, 'POST', path)
}

/**
 * A GET or POST against a path that carries an id.
 *
 * The generated `paths` type keys templated routes by their literal template
 * (`/v1/episodes/{episode_id}`), so a concrete URL is not assignable to it. These two
 * take the template for typing and the built URL for fetching, which keeps the response
 * type generated rather than asserted.
 */
export async function apiGetPath<P extends keyof paths>(
  _template: P,
  url: string,
): Promise<GetResponse<P>> {
  const response = await send(`${apiBaseUrl()}${url}`, { headers: headers() })
  return parse<GetResponse<P>>(response, 'GET', url)
}

export async function apiPostPath<P extends keyof paths>(
  _template: P,
  url: string,
  body?: unknown,
): Promise<PostResponse<P>> {
  const response = await send(`${apiBaseUrl()}${url}`, {
    method: 'POST',
    headers: { ...headers(), 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  })
  return parse<PostResponse<P>>(response, 'POST', url)
}

export async function apiPutPath<P extends keyof paths>(
  _template: P,
  url: string,
  body: unknown,
): Promise<PutResponse<P>> {
  const response = await send(`${apiBaseUrl()}${url}`, {
    method: 'PUT',
    headers: { ...headers(), 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return parse<PutResponse<P>>(response, 'PUT', url)
}

/**
 * A DELETE whose success is 204 and therefore has no body to parse, against a path that
 * carries an id — `apiPostNoContent`'s shape and `apiPostPath`'s typing. The template is
 * there so a call site names a route the contract actually has.
 */
export async function apiDeletePath<P extends keyof paths>(_template: P, url: string): Promise<void> {
  const response = await send(`${apiBaseUrl()}${url}`, { method: 'DELETE', headers: headers() })
  if (!response.ok) throw await refuse(response, 'DELETE', url)
}

/**
 * A DELETE that answers 200 with a body — `apiDeletePath`'s sibling, for the one route
 * whose answer is worth having: revoking a token returns the row it stamped, so the list
 * updates without a second round trip.
 */
export async function apiDeletePathJson<P extends keyof paths>(
  _template: P,
  url: string,
): Promise<DeleteResponse<P>> {
  const response = await send(`${apiBaseUrl()}${url}`, { method: 'DELETE', headers: headers() })
  return parse<DeleteResponse<P>>(response, 'DELETE', url)
}

export const api = {
  health: () => apiGet('/internal/health'),
  // Signing in. `startLogin` and `completeLogin` are the only two calls in this file that
  // work without a token — they are how a browser holding nothing gets something.
  startLogin: (redirectUri: string) =>
    apiPost('/v1/auth/google/start', { redirect_uri: redirectUri }),
  completeLogin: (state: string, code: string) =>
    apiPost('/v1/auth/google/callback', { state, code }),
  // The third call that works without a token: an MCP client's authorization comes back
  // through Google to a browser that may hold nothing (motet#111).
  completeMcpAuthorization: (state: string, code: string) =>
    apiPost('/v1/auth/mcp/callback', { state, code }),
  session: () => apiGet('/v1/auth/session'),
  logout: () => apiPostNoContent('/v1/auth/logout'),
  // Every session, from any device. The answer to a lost phone: `logout` needs the token
  // you are trying to revoke, so it cannot be the one on the device you no longer hold.
  logoutEverywhere: () => apiPost('/v1/auth/logout-all'),
  // Personal access tokens: the non-interactive way into /v1, for an agent that cannot
  // sign in with Google. `mintApiToken` is the only call in this file whose response
  // carries a credential, and it is the only time that value exists — nothing can return
  // it again. All three need a signed-in session, so they are unreachable with a token.
  apiTokens: () => apiGet('/v1/auth/tokens'),
  mintApiToken: (label: string, expiresInDays: number | null) =>
    apiPost('/v1/auth/tokens', { label, expires_in_days: expiresInDays }),
  revokeApiToken: (id: string) =>
    apiDeletePathJson('/v1/auth/tokens/{token_id}', `/v1/auth/tokens/${encodeURIComponent(id)}`),
  newsItems: () => apiGet('/v1/news-items'),
  // One story with every source behind it — titles, arrival dates and the opening of each
  // one. The whole of a source stays on `sourceItem`, which also carries the pipeline.
  newsItem: (id: string) =>
    apiGetPath('/v1/news-items/{news_item_id}', `/v1/news-items/${encodeURIComponent(id)}`),
  // What has been pasted but is not a news item yet. The backlog cannot answer that:
  // an item that fails never becomes a news item, so it never appears there at all.
  ingestion: () => apiGet('/v1/ingestion'),
  // Whether anything is draining the queues. The companion to `ingestion`, which says
  // what is waiting and cannot say whether anything is coming for it (motet#38).
  processing: () => apiGet('/v1/processing'),
  episodes: () => apiGet('/v1/episodes'),
  feed: () => apiGet('/v1/feed'),
  paste: (title: string, text: string) => apiPost('/v1/sources/paste', { title, text }),
  sources: () => apiGet('/v1/sources'),
  // `provider` is sent explicitly even though the API defaults it, so that the one place
  // the SPA names a provider is here rather than buried in a default two repos away.
  // Anything but 'gmail' is a 400: X bookmarks are not built, and there is deliberately
  // no affordance for them.
  // Credentials (motet#102): the sites enrichment may fetch from, and the MCP servers it may
  // use. No call here ever receives a secret back; `has_secret` is the whole of what the API
  // says about one. Authorizing a server is a consent flow like connecting a mailbox, and
  // comes back on the same callback path with a `connector.` state.
  connectors: () => apiGet('/v1/connectors'),
  createConnector: (body: CreateConnector) => apiPost('/v1/connectors', body),
  deleteConnector: (id: string) =>
    apiDeletePath('/v1/connectors/{connector_id}', `/v1/connectors/${encodeURIComponent(id)}`),
  authorizeConnector: (id: string, redirectUri: string) =>
    apiPostPath(
      '/v1/connectors/{connector_id}/authorize',
      `/v1/connectors/${encodeURIComponent(id)}/authorize`,
      { redirect_uri: redirectUri },
    ),
  completeConnectorOAuth: (state: string, code: string, iss?: string) =>
    apiPost('/v1/connectors/oauth/callback', { state, code, iss: iss ?? null }),
  connectSource: (name: string, query: string, redirectUri: string, firstSyncDays: number) =>
    apiPost('/v1/sources/connect', {
      provider: 'gmail',
      name,
      // The API reads null as "use Gmail's own default", which is not the same request as
      // an empty string.
      query: query || null,
      redirect_uri: redirectUri,
      // Always sent, so the window a person was shown on the connect screen is the window
      // they get — rather than whatever the worker's environment happens to say.
      first_sync_days: firstSyncDays,
    }),
  completeOAuth: (state: string, code: string) =>
    apiPost('/v1/sources/callback', { state, code }),
  // What a connected source has pulled in and is holding for an explicit "ingest now".
  heldSourceItems: () => apiGet('/v1/source-items/held'),
  // "Ingest now": the first call in the SPA that spends inference on a polled item. Ids
  // that are no longer held — a second tab got there first — come back as `skipped`.
  integrateSourceItems: (ids: string[]) => apiPost('/v1/source-items/integrate', { ids }),
  // The other way out of the held list: discard without spending anything.
  dismissSourceItems: (ids: string[]) => apiPost('/v1/source-items/dismiss', { ids }),
  // One source item's life as three stages — pulled in, processed, news item.
  sourceItem: (id: string) =>
    apiGetPath(
      '/v1/source-items/{source_item_id}',
      `/v1/source-items/${encodeURIComponent(id)}`,
    ),
  // "Sync now". Enqueues a poll and answers with the source as it stands — it does not
  // fetch, so `last_sync` in the answer is the *previous* poll's; a caller that wants to
  // know the sync ran watches `sources()` for `last_sync.at` to move.
  pollSource: (id: string) =>
    apiPostPath('/v1/sources/{source_id}/poll', `/v1/sources/${encodeURIComponent(id)}/poll`),
  // Forget a mailbox's credential and stop polling it. The source row and everything it
  // pulled in survive — the API says why: claims cite those source items.
  disconnectSource: (id: string) =>
    apiDeletePath(
      '/v1/sources/{source_id}/credentials',
      `/v1/sources/${encodeURIComponent(id)}/credentials`,
    ),
  // Dismiss a consent attempt that never finished. The API refuses (409) for anything
  // that holds or ever held a credential, or has pulled an item in: the delete cascades.
  removeSource: (id: string) =>
    apiDeletePath('/v1/sources/{source_id}', `/v1/sources/${encodeURIComponent(id)}`),
  // Label sync (motet#96). Setting labels widens nothing: a read-only mailbox answers
  // `needs_reauthorization` until its owner goes through `reauthorizeSource`, which is the
  // only call in this file that asks Google for more than read-only access. An empty
  // string is sent as null, because both empty is how label sync is turned off.
  setLabelSync: (id: string, removeLabel: string, addLabel: string) =>
    apiPutPath(
      '/v1/sources/{source_id}/label-sync',
      `/v1/sources/${encodeURIComponent(id)}/label-sync`,
      { remove_label: removeLabel || null, add_label: addLabel || null },
    ),
  // Search a mailbox again from `days` ago, and keep that as its window (motet#139).
  // The repair for a first sync that did not reach far enough: widening the window alone
  // changes nothing, because it is read only where a search begins. Already-ingested mail
  // is skipped before it is fetched, so this cannot duplicate anything.
  resyncSource: (id: string, days: number) =>
    apiPostPath(
      '/v1/sources/{source_id}/resync',
      `/v1/sources/${encodeURIComponent(id)}/resync`,
      { first_sync_days: days },
    ),
  reauthorizeSource: (id: string, redirectUri: string) =>
    apiPostPath(
      '/v1/sources/{source_id}/reauthorize',
      `/v1/sources/${encodeURIComponent(id)}/reauthorize`,
      { redirect_uri: redirectUri },
    ),
  // No title: the server names an episode after the day it was made, and it is the only
  // place that string is composed. `renameEpisode` is how it gets a better one.
  createEpisode: (maxDurationMs: number) => apiPost('/v1/episodes', { max_duration_ms: maxDurationMs }),
  renameEpisode: (id: string, title: string) =>
    apiPutPath(
      '/v1/episodes/{episode_id}/title',
      `/v1/episodes/${encodeURIComponent(id)}/title`,
      { title },
    ),
  rotateFeed: () => apiPost('/v1/feed/rotate'),
  setRead: (id: string, read: boolean) =>
    apiPostPath(
      '/v1/news-items/{news_item_id}/read',
      `/v1/news-items/${encodeURIComponent(id)}/read`,
      { read },
    ),
  markListened: (id: string) =>
    apiPostPath(
      '/v1/episodes/{episode_id}/listened',
      `/v1/episodes/${encodeURIComponent(id)}/listened`,
    ),
  // The operator view, across every user. Admins only: the API answers 403 to anybody
  // else, and the SPA only links to it when `/v1/auth/session` says `admin`. `before` is
  // the previous page's `jobs_next_before`; the aggregates ignore both options.
  adminOverview: (options: { userId?: string | null; before?: number | null } = {}) => {
    const query = new URLSearchParams()
    if (options.userId) query.set('user_id', options.userId)
    if (options.before) query.set('before', String(options.before))
    const suffix = query.toString() ? `?${query.toString()}` : ''
    return apiGetPath('/v1/admin/overview', `/v1/admin/overview${suffix}`)
  },
  // Everyone who joined the waitlist from the landing page, newest first. Admins only, like
  // the overview; `before` is the previous page's `next_before`.
  adminWaitlist: (options: { before?: number | null } = {}) => {
    const suffix = options.before ? `?before=${options.before}` : ''
    return apiGetPath('/v1/admin/waitlist', `/v1/admin/waitlist${suffix}`)
  },
  // How far the listener has got: the position resource a syncing player writes, which
  // is the same handler as `POST …/progress` under the name AGENTS.md gives new callers.
  // Monotonic on the server, so a lower report is a no-op rather than a rewind, and every
  // story whose segment has been passed is marked read.
  setPosition: (id: string, listenedThroughMs: number) =>
    apiPutPath(
      '/v1/episodes/{episode_id}/position',
      `/v1/episodes/${encodeURIComponent(id)}/position`,
      { listened_through_ms: Math.max(0, Math.round(listenedThroughMs)) },
    ),
  /**
   * The episode's audio, as a URL a media element can load directly.
   *
   * Not fetched: a deployed API answers this route with a 307 to a signed object-storage
   * URL on another origin, which a `<audio>` element follows without CORS and a `fetch`
   * does not. The route takes the *feed* token rather than the bearer header, because a
   * media element cannot send one — the same reason the RSS enclosure carries it.
   */
  audioUrl: (id: string, feedToken: string) =>
    `${apiBaseUrl()}/v1/episodes/${encodeURIComponent(id)}/audio?token=${encodeURIComponent(feedToken)}`,
  // Why a media element could not load the audio route, which the element itself cannot
  // say: `error` carries no status. Asked only after a failure. `redirect: 'manual'` stops at
  // the API's own answer — a redirect means the object is there and the failure was the
  // browser's — and the body is never read, so a local backend's 200 is not downloaded.
  // Null when the route did not refuse, or could not be asked within ten seconds.
  audioProblem: async (id: string, feedToken: string): Promise<string | null> => {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 10_000)
    try {
      const response = await fetch(api.audioUrl(id, feedToken), {
        redirect: 'manual',
        signal: controller.signal,
      })
      // The feed token in the URL is refused: rotated while this page was open.
      if (response.status === 401) return 'The private feed link has changed since this page loaded. Reload to play.'
      if (response.status !== 404 && response.status !== 410) return null
      const body = (await response.json().catch(() => null)) as { detail?: unknown } | null
      return typeof body?.detail === 'string' ? body.detail : 'The API has no audio for this episode.'
    } catch {
      return null
    } finally {
      clearTimeout(timer)
      controller.abort()
    }
  },
  // Play Live. Asked first, so an environment with no voice service shows a disabled
  // button with a reason and never reaches for a host that does not exist (motet#93).
  voiceStatus: () => apiGet('/v1/voice'),
  // The API assembles the episode's context and mints the session with the voice service's
  // start token; the browser gets back only a socket URL and the frame to open it with.
  startVoiceSession: (id: string, spokenThroughMs: number) =>
    apiPostPath(
      '/v1/episodes/{episode_id}/voice-session',
      `/v1/episodes/${encodeURIComponent(id)}/voice-session`,
      { spoken_through_ms: Math.max(0, Math.round(spokenThroughMs)) },
    ),
  // Models and spend (motet#92). Admins only, like the overview. `setLlmConfig` is also
  // refused with 409 wherever the deployment does not honour settings — production.
  llmConfig: () => apiGet('/v1/admin/llm-config'),
  setLlmConfig: (stage: string, body: LlmStageUpdate) =>
    apiPutPath('/v1/admin/llm-config/{stage}', `/v1/admin/llm-config/${encodeURIComponent(stage)}`, body),
  llmSpend: () => apiGet('/v1/admin/llm-spend'),
}
