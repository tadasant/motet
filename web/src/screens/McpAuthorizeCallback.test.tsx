import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { McpAuthorization } from '../api/client'
import type { OAuthCallback } from '../oauth'
import { McpAuthorizeCallback } from './McpAuthorizeCallback'

const AUTHORIZATION: McpAuthorization = {
  client_name: 'Claude Desktop',
  redirect_host: 'claude.example',
  email: 'owner@motet.test',
  redirect_url: 'https://claude.example/callback?code=mcp_code_1&state=client_st',
  deny_url: 'https://claude.example/callback?error=access_denied&state=client_st',
}

const GRANTED: OAuthCallback = { kind: 'granted', code: 'abc123', state: 'mcp.st_1' }

type Call = { url: string; method: string; body: unknown }

/** A fetch that answers the MCP callback route with `status`, and remembers what it was asked. */
function mockFetch(status = 200, detail = 'Refused.'): Call[] {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://api.test').pathname
      const method = init?.method ?? 'GET'
      calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
      const ok = url === '/v1/auth/mcp/callback' && status < 400
      return {
        ok,
        status: ok ? 200 : status,
        statusText: ok ? 'OK' : 'Refused',
        json: async () => (ok ? AUTHORIZATION : { detail }),
      } as Response
    }),
  )
  return calls
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('McpAuthorizeCallback', () => {
  it('exchanges the code once, even under StrictMode', async () => {
    // The state row is single-use: a second exchange would answer "already used" over the
    // top of the question the person is meant to be reading.
    const calls = mockFetch()
    render(
      <StrictMode>
        <McpAuthorizeCallback callback={GRANTED} onDone={() => {}} navigate={() => {}} />
      </StrictMode>,
    )

    await screen.findByText('Claude Desktop')
    const exchanges = calls.filter((call) => call.url === '/v1/auth/mcp/callback')
    expect(exchanges).toHaveLength(1)
    expect(exchanges[0]).toMatchObject({ method: 'POST', body: { state: 'mcp.st_1', code: 'abc123' } })
  })

  it('says which client is asking, where the grant goes, and which account it acts as', async () => {
    mockFetch()
    render(<McpAuthorizeCallback callback={GRANTED} onDone={() => {}} navigate={() => {}} />)

    expect(await screen.findByText('Claude Desktop')).toBeDefined()
    expect(screen.getByText(/wants to use Motet as owner@motet.test/)).toBeDefined()
    expect(screen.getByText(/sends you back to claude\.example/)).toBeDefined()
  })

  it('never sends the browser anywhere before a click', async () => {
    // The code is already inside `redirect_url`. Navigating there is the grant.
    mockFetch()
    const navigate = vi.fn()
    render(<McpAuthorizeCallback callback={GRANTED} onDone={() => {}} navigate={navigate} />)

    await screen.findByRole('button', { name: 'Allow' })
    // Give any stray effect a chance to run before asserting it did not.
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(navigate).not.toHaveBeenCalled()
  })

  it('hands the grant to the client on Allow', async () => {
    mockFetch()
    const navigate = vi.fn()
    render(<McpAuthorizeCallback callback={GRANTED} onDone={() => {}} navigate={navigate} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Allow' }))

    expect(navigate).toHaveBeenCalledTimes(1)
    expect(navigate).toHaveBeenCalledWith(AUTHORIZATION.redirect_url)
    // On its way out: a second press must not send the browser anywhere again.
    fireEvent.click(screen.getByRole('button', { name: 'Deny' }))
    expect(navigate).toHaveBeenCalledTimes(1)
  })

  it('tells the client no on Deny, without the code', async () => {
    mockFetch()
    const navigate = vi.fn()
    render(<McpAuthorizeCallback callback={GRANTED} onDone={() => {}} navigate={navigate} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Deny' }))

    expect(navigate).toHaveBeenCalledTimes(1)
    expect(navigate).toHaveBeenCalledWith(AUTHORIZATION.deny_url)
  })

  it("shows the API's own sentence when it refuses", async () => {
    mockFetch(403, 'That Google account is not allowed to use this Motet.')
    const navigate = vi.fn()
    const onDone = vi.fn()
    render(<McpAuthorizeCallback callback={GRANTED} onDone={onDone} navigate={navigate} />)

    expect((await screen.findByRole('alert')).textContent).toMatch(
      /That Google account is not allowed to use this Motet\./,
    )
    expect(screen.queryByRole('button', { name: 'Allow' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Back to Motet' }))
    expect(onDone).toHaveBeenCalled()
    expect(navigate).not.toHaveBeenCalled()
  })

  it('reads a refusal at Google as an answer, and asks the API nothing', async () => {
    const calls = mockFetch()
    const navigate = vi.fn()
    render(
      <McpAuthorizeCallback
        callback={{ kind: 'denied', error: 'access_denied', description: '', state: 'mcp.st_1' }}
        onDone={() => {}}
        navigate={navigate}
      />,
    )

    expect(screen.getByRole('status').textContent).toMatch(/did not finish signing in/)
    expect(screen.getByRole('button', { name: 'Back to Motet' })).toBeDefined()
    await waitFor(() => expect(calls).toHaveLength(0))
    expect(navigate).not.toHaveBeenCalled()
  })
})

describe('McpAuthorizeCallback with a hostile client', () => {
  it('offers no button when the client registered a redirect that would run script here', async () => {
    // A stranger can register any client; `location.assign` on a `javascript:` URI would run
    // their script on this origin whichever button the person pressed.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        status: 200,
        statusText: 'OK',
        json: async () => ({
          ...AUTHORIZATION,
          redirect_url: 'javascript:alert(document.domain)//?code=mcp_code_1',
          deny_url: 'javascript:alert(document.domain)//?error=access_denied',
        }),
      })) as unknown as typeof fetch,
    )
    const navigate = vi.fn()
    render(<McpAuthorizeCallback callback={GRANTED} onDone={() => {}} navigate={navigate} />)

    expect((await screen.findByRole('alert')).textContent).toContain('will not open')
    expect(screen.queryByRole('button', { name: 'Allow' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Deny' })).toBeNull()
    expect(navigate).not.toHaveBeenCalled()
  })
})
