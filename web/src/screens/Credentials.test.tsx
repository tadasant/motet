// The Credentials screen (motet#102).
//
// What is pinned: the empty states say what each kind is for; a site is added from its
// domain alone, normalized; a password without a username cannot be submitted; an MCP
// server cannot be added until the risk box is ticked, and the request says it was; an
// unauthorized server offers Authorize, which starts consent through the API and remembers
// the state; a server without registration shows the API's reason; Remove removes.

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Connector } from '../api/client'
import { isConnectorState } from '../oauth'
import { Credentials } from './Credentials'
import { pillFor } from './credentials/ConnectorRow'
import { looksLikeDomain, normalizeDomain } from './credentials/domain'

const SITE: Connector = {
  id: 'cn_site',
  kind: 'site',
  label: 'example.com',
  domain: 'example.com',
  domains: [],
  url: null,
  username: null,
  has_secret: false,
  secret_expires_at: null,
  oauth_issuer: null,
  oauth_registered: false,
  risk_acknowledged_at: null,
  status: 'ready',
  last_error: null,
  created_at: '2026-09-13T00:00:00Z',
  updated_at: '2026-09-13T00:00:00Z',
}

const MCP: Connector = {
  ...SITE,
  id: 'cn_mcp',
  kind: 'mcp',
  label: 'Mail (read-only)',
  domain: null,
  url: 'https://mcp.example/mcp?servers=mail-ro',
  risk_acknowledged_at: '2026-09-13T00:00:00Z',
  status: 'needs_auth',
}

/** Route a fake fetch by `METHOD url` first, then by URL prefix (longest wins). */
function mockApi(overrides: Record<string, unknown> = {}) {
  const calls: { url: string; method: string; body: unknown }[] = []
  const routes: Record<string, unknown> = { '/v1/connectors': [], ...overrides }
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    // The API base is empty in tests, so the URL is a bare path; resolve it before reading.
    const path = new URL(url, window.location.origin).pathname
    const method = init?.method ?? 'GET'
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
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

describe('domains', () => {
  it('reduces a pasted URL to the site and drops www', () => {
    expect(normalizeDomain('https://www.Example.com/articles/x?y=1')).toBe('example.com')
    expect(normalizeDomain('  example.com. ')).toBe('example.com')
    expect(normalizeDomain('user@host.example:8443/')).toBe('host.example')
  })

  it('accepts a host name and refuses an address or a word', () => {
    expect(looksLikeDomain('news.example.co.uk')).toBe(true)
    expect(looksLikeDomain('10.0.0.1')).toBe(false)
    expect(looksLikeDomain('nodot')).toBe(false)
  })
})

describe('the row pill', () => {
  it('says what a site will log in with', () => {
    expect(pillFor(SITE).text).toBe('Ready · no login')
    expect(pillFor({ ...SITE, username: 'r@example.net' }).text).toBe('Ready · passwordless login')
    expect(pillFor({ ...SITE, username: 'r@example.net', has_secret: true }).text).toBe(
      'Ready · login saved',
    )
    expect(pillFor(MCP).text).toBe('Needs authorization')
  })
})

describe('the Credentials screen', () => {
  it('explains what each kind is for when there is nothing yet', async () => {
    mockApi()
    render(<Credentials navigate={vi.fn()} />)
    const notes = await screen.findAllByRole('status')
    expect(notes.map((n) => n.textContent)).toEqual([
      expect.stringMatching(/No sites yet.*login is optional/s),
      expect.stringMatching(/No MCP servers yet.*Only OAuth-secured servers/s),
    ])
    expect(screen.getByText(/Nothing is fetched until enrichment runs/)).toBeDefined()
  })

  it('adds a site from its domain alone, normalized', async () => {
    const calls = mockApi({ 'POST /v1/connectors': SITE })
    render(<Credentials navigate={vi.fn()} />)
    await screen.findAllByRole('status')

    const domain = screen.getByLabelText('Domain')
    fireEvent.change(domain, { target: { value: 'https://www.Example.com/newsletters' } })
    fireEvent.blur(domain)
    expect((domain as HTMLInputElement).value).toBe('example.com')
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))

    await waitFor(() => expect(screen.getByRole('list', { name: 'Sites' })).toBeDefined())
    const post = calls.find((c) => c.method === 'POST')
    expect(post?.body).toEqual({
      kind: 'site',
      label: '',
      domain: 'example.com',
      username: null,
      password: null,
      acknowledge_risk: false,
    })
    const row = within(screen.getByRole('list', { name: 'Sites' })).getByRole('listitem')
    expect(row.textContent).toContain('Ready · no login')
  })

  it('will not submit a password without the username it belongs to', async () => {
    mockApi()
    render(<Credentials navigate={vi.fn()} />)
    await screen.findAllByRole('status')
    fireEvent.change(screen.getByLabelText('Domain'), { target: { value: 'example.com' } })
    fireEvent.change(screen.getByLabelText('Password (optional)'), { target: { value: 'pw' } })
    expect((screen.getByRole('button', { name: 'Add' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Username or email (optional)'), {
      target: { value: 'reader@example.net' },
    })
    expect((screen.getByRole('button', { name: 'Add' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('shows the risk and will not add an MCP server until it is acknowledged', async () => {
    const calls = mockApi({ 'POST /v1/connectors': MCP })
    render(<Credentials navigate={vi.fn()} />)
    await screen.findAllByRole('status')

    fireEvent.click(screen.getByRole('radio', { name: 'MCP server' }))
    const risk = screen.getByRole('note', { name: 'Before you connect a server' })
    expect(risk.textContent).toMatch(/hostile page can instruct it to use this server/)
    expect(risk.textContent).toMatch(/does not close it/)

    fireEvent.change(screen.getByLabelText('Server URL'), {
      target: { value: 'https://mcp.example/mcp?servers=mail-ro' },
    })
    const add = screen.getByRole('button', { name: 'Add' }) as HTMLButtonElement
    expect(add.disabled).toBe(true)
    fireEvent.click(within(risk).getByRole('checkbox'))
    expect(add.disabled).toBe(false)
    fireEvent.click(add)

    await waitFor(() => expect(screen.getByRole('list', { name: 'MCP servers' })).toBeDefined())
    const post = calls.find((c) => c.method === 'POST')
    expect(post?.body).toEqual({
      kind: 'mcp',
      label: '',
      url: 'https://mcp.example/mcp?servers=mail-ro',
      domains: [],
      acknowledge_risk: true,
    })
  })

  it('offers Authorize for an unauthorized server and starts consent through the API', async () => {
    const navigate = vi.fn()
    const calls = mockApi({
      '/v1/connectors': [MCP],
      'POST /v1/connectors/cn_mcp/authorize': {
        authorization_url: 'https://mcp.example/oauth/authorize?state=connector.abc',
        state: 'connector.abc',
      },
    })
    render(<Credentials navigate={navigate} />)
    const row = within(await screen.findByRole('list', { name: 'MCP servers' })).getByRole('listitem')
    expect(row.textContent).toContain('Needs authorization')
    expect(row.textContent).toContain('for every site you added')

    fireEvent.click(within(row).getByRole('button', { name: 'Authorize' }))
    await waitFor(() => expect(navigate).toHaveBeenCalledOnce())
    expect(navigate).toHaveBeenCalledWith('https://mcp.example/oauth/authorize?state=connector.abc')
    const authorize = calls.find((c) => c.url.endsWith('/authorize'))
    expect(authorize?.body).toEqual({ redirect_uri: `${window.location.origin}/oauth/callback` })
    const remembered = window.sessionStorage.getItem('motet.oauthState')
    expect(remembered).toBe('connector.abc')
    expect(isConnectorState(remembered ?? '')).toBe(true)
  })

  it('shows the reason when the server cannot register a client', async () => {
    const reason =
      'https://mcp.example does not offer dynamic client registration. Authorizing it needs a client id registered by hand with that server, which Motet does not support.'
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
    const row = within(await screen.findByRole('list', { name: 'Sites' })).getByRole('listitem')
    fireEvent.click(within(row).getByRole('button', { name: 'Remove' }))
    await screen.findByText(/No sites yet/)
    expect(calls.some((c) => c.method === 'DELETE' && c.url.endsWith('/cn_site'))).toBe(true)
  })

  it('says a failed load is not an empty list', async () => {
    mockApi({ '/v1/connectors': { status: 503, detail: 'down' } })
    render(<Credentials navigate={vi.fn()} />)
    expect((await screen.findByRole('alert')).textContent).toMatch(/not the same as having none/)
  })
})
