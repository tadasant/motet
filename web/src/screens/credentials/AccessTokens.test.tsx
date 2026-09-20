// Access tokens on the Credentials screen.
//
// What is pinned is the part a person can get wrong once and never recover from: the
// minted token is shown, it is shown only in the mint response, and it never appears
// again once the panel is dismissed. Plus the ordinary shape — the list, revoking, and
// the panel not being offered to a caller the API would refuse.

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ApiToken } from '../../api/client'
import { Credentials } from '../Credentials'
import { AccessTokens } from './AccessTokens'

const LIVE: ApiToken = {
  id: 'pat_1',
  prefix: 'mot_stg_Ab3dEf7h',
  label: 'staging agent',
  email: 'owner@motet.test',
  created_at: '2026-09-20T00:00:00Z',
  last_used_at: null,
  expires_at: null,
  revoked_at: null,
}

const SECRET = 'mot_stg_Ab3dEf7hIjKlMnOpQrStUvWxYz0123456789abcd'

function mockApi(overrides: Record<string, unknown> = {}) {
  const calls: { path: string; method: string; body: unknown }[] = []
  const routes: Record<string, unknown> = {
    '/v1/connectors': [],
    '/v1/auth/tokens': [],
    ...overrides,
  }
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), window.location.origin).pathname
      const method = init?.method ?? 'GET'
      calls.push({ path, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
      const names = Object.keys(routes)
      const key =
        names.filter((r) => r.includes(' ')).find((r) => `${method} ${path}` === r) ??
        names
          .filter((r) => !r.includes(' '))
          .sort((a, b) => b.length - a.length)
          .find((r) => path.startsWith(r))
      const value = key === undefined ? undefined : routes[key]
      if (typeof value === 'object' && value !== null && 'status' in value && 'detail' in value) {
        const failure = value as { status: number; detail: string }
        return {
          ok: false,
          status: failure.status,
          statusText: 'Error',
          json: async () => ({ detail: failure.detail }),
        } as Response
      }
      return {
        ok: key !== undefined,
        status: key === undefined ? 404 : 200,
        statusText: 'OK',
        json: async () => (key === undefined ? { detail: 'not found' } : value),
      } as Response
    }),
  )
  return calls
}

beforeEach(() => {
  window.localStorage.clear()
  window.localStorage.setItem('motet.apiToken', 'test-token')
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('AccessTokens', () => {
  it('lists a token by its prefix and label, never by its value', async () => {
    mockApi({ '/v1/auth/tokens': [LIVE] })
    render(<AccessTokens />)

    const list = await screen.findByRole('list', { name: 'Access tokens' })
    expect(within(list).getByText('staging agent')).toBeTruthy()
    expect(within(list).getByText(`${LIVE.prefix}…`)).toBeTruthy()
    expect(within(list).getByText('Live')).toBeTruthy()
  })

  it('shows a minted token once, with the warning, and then forgets it', async () => {
    mockApi({
      'POST /v1/auth/tokens': { token: SECRET, created: LIVE },
      '/v1/auth/tokens': [],
    })
    render(<AccessTokens />)
    await screen.findByText('No tokens yet.')

    fireEvent.change(screen.getByLabelText(/What is it for/), {
      target: { value: 'staging agent' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Create token' }))

    expect(await screen.findByText(SECRET)).toBeTruthy()
    expect(screen.getByText(/It will not be shown again/)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Done' }))
    await waitFor(() => expect(screen.queryByText(SECRET)).toBeNull())
  })

  it('sends the chosen lifetime, and null by default', async () => {
    const calls = mockApi({ 'POST /v1/auth/tokens': { token: SECRET, created: LIVE } })
    render(<AccessTokens />)
    await screen.findByText('No tokens yet.')

    fireEvent.change(screen.getByLabelText(/What is it for/), { target: { value: 'a' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create token' }))
    await screen.findByText(SECRET)
    expect(calls.find((c) => c.method === 'POST')?.body).toEqual({
      label: 'a',
      expires_in_days: null,
    })

    fireEvent.change(screen.getByLabelText('Expires'), { target: { value: '90' } })
    fireEvent.change(screen.getByLabelText(/What is it for/), { target: { value: 'b' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create token' }))
    await waitFor(() =>
      expect(calls.filter((c) => c.method === 'POST').at(-1)?.body).toEqual({
        label: 'b',
        expires_in_days: 90,
      }),
    )
  })

  it('revokes a token and marks the row rather than removing it', async () => {
    const revoked = { ...LIVE, revoked_at: '2026-09-21T00:00:00Z' }
    const calls = mockApi({
      '/v1/auth/tokens': [LIVE],
      'DELETE /v1/auth/tokens/pat_1': revoked,
    })
    render(<AccessTokens />)
    fireEvent.click(await screen.findByRole('button', { name: 'Revoke' }))

    expect(await screen.findByText('Revoked')).toBeTruthy()
    expect(calls.some((c) => c.method === 'DELETE' && c.path === '/v1/auth/tokens/pat_1')).toBe(
      true,
    )
    // Still listed: the audit question is "when did it stop", not "does it exist".
    expect(screen.getByText('staging agent')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Revoke' })).toBeNull()
  })

  it('says it is loading rather than rendering an empty list', async () => {
    let release: (value: unknown) => void = () => undefined
    const gate = new Promise((resolve) => {
      release = resolve
    })
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        await gate
        return { ok: true, status: 200, statusText: 'OK', json: async () => [] } as Response
      }),
    )
    render(<AccessTokens />)

    expect(await screen.findByText('Loading…')).toBeTruthy()
    expect(screen.queryByRole('list', { name: 'Access tokens' })).toBeNull()

    release(undefined)
    expect(await screen.findByText('No tokens yet.')).toBeTruthy()
  })

  it('keeps a token minted while a revoke was in flight', async () => {
    // The revoke handler reads the list functionally rather than off its render closure:
    // the refresh a mint kicks off can land mid-flight, and the closure would undo it.
    const second: ApiToken = { ...LIVE, id: 'pat_2', label: 'second', prefix: 'mot_stg_ZZZZ1111' }
    const revoked = { ...LIVE, revoked_at: '2026-09-21T00:00:00Z' }
    mockApi({
      '/v1/auth/tokens': [LIVE, second],
      'DELETE /v1/auth/tokens/pat_1': revoked,
    })
    render(<AccessTokens />)
    const rows = await screen.findByRole('list', { name: 'Access tokens' })
    const [firstRevoke] = within(rows).getAllByRole('button', { name: 'Revoke' })
    if (!firstRevoke) throw new Error('expected a Revoke button on the first row')
    fireEvent.click(firstRevoke)

    expect(await screen.findByText('Revoked')).toBeTruthy()
    // The other row is still there — the update touched one entry, not the whole list.
    expect(screen.getByText('second')).toBeTruthy()
  })

  it('says so when the list cannot be loaded, rather than reading as empty', async () => {
    mockApi({ '/v1/auth/tokens': { status: 503, detail: 'the database is not configured' } })
    render(<AccessTokens />)
    expect(await screen.findByText(/This is not the same as having none/)).toBeTruthy()
  })
})

describe('Credentials offers the panel only to a signed-in caller', () => {
  it('is absent for the shared API token', async () => {
    mockApi()
    render(<Credentials signedIn={false} />)
    await screen.findByText('Sites')
    expect(screen.queryByRole('heading', { name: 'Access tokens' })).toBeNull()
  })

  it('is present for a session', async () => {
    mockApi()
    render(<Credentials signedIn />)
    expect(await screen.findByRole('heading', { name: 'Access tokens' })).toBeTruthy()
  })
})
