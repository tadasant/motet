// Where the SPA thinks the API lives.
//
// This is the piece that only breaks once the thing is deployed: on a laptop the Vite
// dev server proxies /v1 and same-origin is correct, so a build-time-only base URL looks
// fine right up until `app.` and `api.` are two different hostnames.

import { afterEach, describe, expect, it } from 'vitest'

import { apiBaseUrl } from './client'

afterEach(() => {
  delete window.__MOTET_CONFIG__
})

describe('apiBaseUrl', () => {
  it('is same-origin when nothing is configured', () => {
    // What `npm run dev` wants: vite.config.ts proxies /v1 to the local API.
    expect(apiBaseUrl()).toBe('')
  })

  it('uses the origin the container was started with', () => {
    window.__MOTET_CONFIG__ = { apiBaseUrl: 'https://api.example.invalid' }
    expect(apiBaseUrl()).toBe('https://api.example.invalid')
  })

  it('is read at call time, not frozen at module load', () => {
    // config.js is a separate script tag, so the bundle can evaluate before it in some
    // loading orders. Reading per call is what keeps that from baking in an empty value.
    expect(apiBaseUrl()).toBe('')
    window.__MOTET_CONFIG__ = { apiBaseUrl: 'https://late.example.invalid' }
    expect(apiBaseUrl()).toBe('https://late.example.invalid')
  })

  it('strips a trailing slash', () => {
    // Paths already start with one, and `https://host//v1/...` is a different path to
    // some routers.
    window.__MOTET_CONFIG__ = { apiBaseUrl: 'https://api.example.invalid/' }
    expect(apiBaseUrl()).toBe('https://api.example.invalid')
  })

  it('treats a blank value as unconfigured', () => {
    // The placeholder config.js ships `apiBaseUrl: ''`, and an unset MOTET_API_BASE_URL
    // leaves it that way.
    window.__MOTET_CONFIG__ = { apiBaseUrl: '   ' }
    expect(apiBaseUrl()).toBe('')
  })
})
