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

export type HealthResponse = GetResponse<'/healthz'>

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

export async function apiGet<P extends keyof paths>(path: P): Promise<GetResponse<P>> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) {
    throw new ApiError(response.status, `GET ${path} failed with ${response.status}`)
  }
  return (await response.json()) as GetResponse<P>
}
