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

export type HealthResponse = GetResponse<'/healthz'>
export type NewsItem = GetResponse<'/v1/news-items'>[number]
export type Episode = GetResponse<'/v1/episodes'>[number]
export type EpisodeSegment = Episode['segments'][number]
export type Claim = EpisodeSegment['claims'][number]
export type FeedInfo = GetResponse<'/v1/feed'>
export type SourceItem = PostResponse<'/v1/sources/paste'>

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
 * The API base URL, injected at build time.
 *
 * Empty by default so the dev server and the deployed SPA both use same-origin relative
 * paths. Real hostnames live in the private infrastructure repo, never in this tree.
 */
export const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? ''

const TOKEN_STORAGE_KEY = 'motet.apiToken'

/**
 * The API token, kept in localStorage.
 *
 * Phase 1 has one hardcoded account and no signup, so this is a shared secret typed in
 * once rather than a session. It is a lock on the door: without it a deployed API is one
 * paste away from spending inference budget for anyone who finds it. Real accounts arrive
 * in Phase 3, and the only thing that changes here is where the token comes from.
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

async function parse<T>(response: Response, method: string, path: string): Promise<T> {
  if (!response.ok) {
    // The API answers with `{"detail": "..."}`; a proxy or a crash might not. Falling back
    // to the status keeps an error message from being the literal string "undefined".
    let detail = `${response.status}`
    try {
      const body = (await response.json()) as { detail?: unknown }
      if (typeof body.detail === 'string') detail = body.detail
    } catch {
      detail = response.statusText || detail
    }
    throw new ApiError(response.status, `${method} ${path} failed: ${detail}`)
  }
  return (await response.json()) as T
}

export async function apiGet<P extends keyof paths>(path: P): Promise<GetResponse<P>> {
  const response = await fetch(`${API_BASE_URL}${path}`, { headers: headers() })
  return parse<GetResponse<P>>(response, 'GET', path)
}

export async function apiPost<P extends keyof paths>(
  path: P,
  body?: unknown,
): Promise<PostResponse<P>> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: 'POST',
    headers: { ...headers(), 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  })
  return parse<PostResponse<P>>(response, 'POST', path)
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
  const response = await fetch(`${API_BASE_URL}${url}`, { headers: headers() })
  return parse<GetResponse<P>>(response, 'GET', url)
}

export async function apiPostPath<P extends keyof paths>(
  _template: P,
  url: string,
  body?: unknown,
): Promise<PostResponse<P>> {
  const response = await fetch(`${API_BASE_URL}${url}`, {
    method: 'POST',
    headers: { ...headers(), 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  })
  return parse<PostResponse<P>>(response, 'POST', url)
}

export const api = {
  health: () => apiGet('/healthz'),
  newsItems: () => apiGet('/v1/news-items'),
  episodes: () => apiGet('/v1/episodes'),
  feed: () => apiGet('/v1/feed'),
  paste: (title: string, text: string) => apiPost('/v1/sources/paste', { title, text }),
  createEpisode: (title: string, maxDurationMs: number) =>
    apiPost('/v1/episodes', { title, max_duration_ms: maxDurationMs }),
  rotateFeed: () => apiPost('/v1/feed/rotate'),
  episode: (id: string) =>
    apiGetPath('/v1/episodes/{episode_id}', `/v1/episodes/${encodeURIComponent(id)}`),
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
}
