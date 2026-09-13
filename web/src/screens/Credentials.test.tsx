// PROTOTYPE — the Credentials screen.
//
// What is pinned: the two empty states say what each kind is for; a site login is added
// with the domain normalized and the password allowed to be empty; an MCP server reads as
// needing authorization and Authorize starts consent through the API and remembers the
// state; a server without registration shows the API's reason; Remove removes.

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Connector } from '../api/client'
import { isConnectorState } from '../oauth'
import { Credentials } from './Credentials'
import { normalizeDomain } from './credentials/domain'

const SITE: Connector = {
  id: 'cn_site',
  kind: 'site',
  label: 'The Information',
  domain: 'theinformation.com',
  domains: [],
  url: null,
  username: 'reader@example.com',
  has_secret: false,
  secret_expires_at: null,
  oauth_issuer: null,
  oauth_registered: false,
  status: 'ready',
  last_error: null,
  created_at: '2026-09-12T00:00:00Z',
  updated_at: '2026-09-12T00:00:00Z',
}

const MCP: Connector = {
  id: 'cn_mcp',
  kind: 'mcp',
  label: 'Email (gmail-ro)',
  domain: null,
  domains: [],
  url: 'https://strad.example/mcp?servers=gmail-ro',
  username: null,
  has_secret: false,
  secret_expires_at: null,
  oauth_issuer: null,
  oauth_registered: false,
  status: 'needs_auth',
  last_error: null,
  created_at: '2026-09-12T00:00:00Z',
  updated_at: '2026-09-12T00:00:00Z',
}

/** Route a fake fetch by `METHOD url` first, then by URL prefix (longest wins). */
function mockApi(overrides: Record<string, unknown> = {}) {
  const calls: { url: string; method: string; body: unknown }[] = []
  const routes: Record<string, unknown> = { '/v1/connectors': [], ...overrides }
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
    const names = Object.keys(routes)
    const key =
      names.filter((r) => r.includes(' ')).find((r) => `${method} ${url}` === r) ??
      names
        .filter((r) => !r.includes(' '))
        .sort((a, b) => b.length - a.length)
        .find((r) => url.startsWith(r))
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
      status: key === undefined ? 404 : value === null ? 204 : 200,
      statusText: 'OK',
      json: async () => (key === undefined ? { detail: 'not found' } : value),
    } as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return calls
}

beforeEach(() => {
  window.localStorage.clear()
  window.sessionStorage.clear()
  window.localStorage.setItem('motet.apiToken', 'test-token')
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('normalizeDomain', () => {
  it('reduces a pasted URL to the host and drops www', () => {
    expect(normalizeDomain('https://www.TheInformation.com/articles/x?y=1')).toBe('theinformation.com')
    expect(normalizeDomain('  example.com. ')).toBe('example.com')
    expect(normalizeDomain('user@host.example:8443/')).toBe('host.example')
  })
})

describe('the Credentials screen', () => {
  it('explains what each kind is for when there is nothing yet', async () => {
    mockApi()
    render(<Credentials navigate={vi.fn()} />)
    const notes = await screen.findAllByRole('status')
    expect(notes.map((n) => n.textContent)).toEqual([
      expect.stringMatching(/No site logins yet.*password is optional/s),
      expect.stringMatching(/No MCP servers yet.*Only OAuth-secured servers/s),
    ])
    expect(screen.getByText(/Used by the ingestion agent/)).toBeDefined()
  })

  it('adds a site login with the domain normalized and no password', async () => {
    const calls = mockApi({ 'POST /v1/connectors': SITE })
    render(<Credentials navigate={vi.fn()} />)
    await screen.findAllByRole('status')

    const domain = screen.getByLabelText('Domain')
    fireEvent.change(domain, { target: { value: 'https://www.TheInformation.com/newsletters' } })
    fireEvent.blur(domain)
    expect((domain as HTMLInputElement).value).toBe('theinformation.com')
    fireEvent.change(screen.getByLabelText('Username or email'), {
      target: { value: 'reader@example.com' },
    })
    expect(screen.getByText('Leave blank for email-code or magic-link logins.')).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))

    await waitFor(() => expect(screen.getByRole('list', { name: 'Site logins' })).toBeDefined())
    const post = calls.find((c) => c.method === 'POST')
    expect(post?.body).toEqual({
      kind: 'site',
      label: 'theinformation.com',
      domain: 'theinformation.com',
      username: 'reader@example.com',
      password: '',
    })
    const row = within(screen.getByRole('list', { name: 'Site logins' })).getByRole('listitem')
    expect(row.textContent).toContain('Ready · no password')
    expect(row.textContent).toContain('reader@example.com')
  })

  it('offers Authorize for an unauthorized MCP server and starts consent through the API', async () => {
    const navigate = vi.fn()
    const calls = mockApi({
      '/v1/connectors': [MCP],
      'POST /v1/connectors/cn_mcp/authorize': {
        authorization_url: 'https://strad.example/oauth/authorize?state=connector.abc',
        state: 'connector.abc',
      },
    })
    render(<Credentials navigate={navigate} />)
    const row = within(await screen.findByRole('list', { name: 'MCP servers' })).getByRole('listitem')
    expect(row.textContent).toContain('Needs authorization')

    fireEvent.click(within(row).getByRole('button', { name: 'Authorize' }))
    await waitFor(() => expect(navigate).toHaveBeenCalledOnce())
    expect(navigate).toHaveBeenCalledWith('https://strad.example/oauth/authorize?state=connector.abc')
    const authorize = calls.find((c) => c.url.endsWith('/authorize'))
    expect(authorize?.body).toEqual({ redirect_uri: `${window.location.origin}/oauth/callback` })
    // The state is remembered for the callback, and it is recognisably a connector's.
    const remembered = window.sessionStorage.getItem('motet.oauthState')
    expect(remembered).toBe('connector.abc')
    expect(isConnectorState(remembered ?? '')).toBe(true)
  })

  it('shows the reason when the server cannot register a client', async () => {
    const reason =
      'https://strad.example does not offer dynamic client registration. Authorizing it needs a client id registered by hand with that server.'
    mockApi({
      '/v1/connectors': [MCP],
      'POST /v1/connectors/cn_mcp/authorize': { status: 409, detail: reason },
    })
    render(<Credentials navigate={vi.fn()} />)
    const row = within(await screen.findByRole('list', { name: 'MCP servers' })).getByRole('listitem')
    fireEvent.click(within(row).getByRole('button', { name: 'Authorize' }))
    expect((await screen.findByRole('alert')).textContent).toContain('registered by hand')
  })

  it('removes a connector', async () => {
    const calls = mockApi({ '/v1/connectors': [SITE], 'DELETE /v1/connectors/cn_site': null })
    render(<Credentials navigate={vi.fn()} />)
    const row = within(await screen.findByRole('list', { name: 'Site logins' })).getByRole('listitem')
    fireEvent.click(within(row).getByRole('button', { name: 'Remove' }))
    await screen.findByText(/No site logins yet/)
    expect(calls.some((c) => c.method === 'DELETE' && c.url.endsWith('/cn_site'))).toBe(true)
  })
})
