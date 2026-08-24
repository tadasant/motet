import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App'
import type { Episode, HealthResponse, NewsItem, Source } from './api/client'
// Imported directly for the connect tests: handing the browser to Google is the one line
// of that flow jsdom cannot execute, and the screen takes it as a prop for that reason.
import { Sources } from './screens/Sources'

const NEWS_ITEM: NewsItem = {
  id: 'ni_1',
  title: 'Acme raises $20M Series A',
  summary: 'Acme announced the round on Tuesday.',
  source_item_ids: ['si_1', 'si_2'],
  read: false,
  created_at: '2026-08-24T00:00:00Z',
}

const EPISODE: Episode = {
  id: 'ep_1',
  title: 'Morning briefing',
  state: 'ready',
  duration_ms: 92_000,
  max_duration_ms: 1_200_000,
  audio_bytes: 51_244,
  audio_media_type: 'audio/mpeg',
  last_error: null,
  created_at: '2026-08-24T00:00:00Z',
  published_at: '2026-08-24T00:01:00Z',
  segments: [
    {
      news_item_id: 'ni_1',
      news_item_title: 'Acme raises $20M Series A',
      text: 'Acme raised twenty million dollars.',
      start_ms: 0,
      duration_ms: 92_000,
      claims: [
        {
          text: 'Acme raised twenty million dollars.',
          span: { source_item_id: 'si_1', start: 0, end: 25 },
          source_excerpt: 'Acme raises $20M Series A',
          source_title: 'Morning Brief',
        },
      ],
    },
  ],
}

const GMAIL_SOURCE: Source = {
  id: 'src_1',
  kind: 'gmail',
  name: 'Gmail',
  // Created inactive and unconnected, and that is correct until consent completes — the
  // screen has to read as "waiting", not as "broken".
  active: false,
  connected: false,
  scopes: [],
  last_polled_at: null,
  last_error: null,
  created_at: '2026-08-24T00:00:00Z',
}

/** Route a fake fetch by URL, so a test asserts on what the SPA actually requested. */
function mockApi(overrides: Record<string, unknown> = {}) {
  const calls: { url: string; method: string; body: unknown }[] = []
  const routes: Record<string, unknown> = {
    '/v1/news-items': [NEWS_ITEM],
    '/v1/feed': { url: 'https://example.test/feed.xml?token=secret', token: 'secret' },
    '/v1/episodes': EPISODE,
    '/v1/sources': [GMAIL_SOURCE],
    ...overrides,
  }
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    calls.push({
      url,
      method: init?.method ?? 'GET',
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    })
    const key = Object.keys(routes)
      .sort((a, b) => b.length - a.length)
      .find((route) => url.startsWith(route))
    return {
      ok: key !== undefined,
      status: key === undefined ? 404 : 200,
      statusText: 'OK',
      json: async () => (key === undefined ? { detail: 'not found' } : routes[key]),
    } as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return calls
}

beforeEach(() => {
  window.localStorage.clear()
  window.sessionStorage.clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
  // The callback tests navigate. Leaving the app on /oauth/callback would put every
  // later test into the callback branch.
  window.history.replaceState({}, '', '/')
})

describe('App', () => {
  it('shows the three Phase 1 screens and starts on paste-in', async () => {
    mockApi()
    render(<App />)
    for (const label of ['Paste in', 'Backlog', 'Episode']) {
      expect(screen.getByRole('button', { name: label })).toBeDefined()
    }
    expect(await screen.findByRole('heading', { name: 'Paste in' })).toBeDefined()
  })

  it('sends the bearer token it was given', async () => {
    window.localStorage.setItem('motet.apiToken', 'shhh')
    mockApi()
    render(<App />)
    await screen.findByRole('heading', { name: 'Paste in' })
    const fetchMock = vi.mocked(fetch)
    const [, init] = fetchMock.mock.calls[0]!
    expect((init as RequestInit).headers).toMatchObject({ Authorization: 'Bearer shhh' })
  })

  it('posts pasted text to the ingestion route', async () => {
    const calls = mockApi({ '/v1/sources/paste': { id: 'si_9', title: 'T', state: 'pending' } })
    render(<App />)
    await screen.findByRole('heading', { name: 'Paste in' })

    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'A title' } })
    fireEvent.change(screen.getByLabelText('Text'), { target: { value: 'Some newsletter.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Ingest' }))

    await screen.findByText(/Queued as si_9/)
    const paste = calls.find((call) => call.url.includes('/v1/sources/paste'))
    expect(paste?.method).toBe('POST')
    expect(paste?.body).toEqual({ title: 'A title', text: 'Some newsletter.' })
  })

  it('lists the backlog and toggles read state per news item', async () => {
    const calls = mockApi({
      '/v1/news-items/ni_1/read': { ...NEWS_ITEM, read: true },
    })
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: 'Backlog' }))

    expect(await screen.findByText('Acme raises $20M Series A')).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Mark read' }))

    await waitFor(() => {
      const read = calls.find((call) => call.url.includes('/read'))
      expect(read?.body).toEqual({ read: true })
    })
  })

  it('creates an episode from the backlog and opens it', async () => {
    const calls = mockApi()
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: 'Backlog' }))
    await screen.findByText('Acme raises $20M Series A')

    fireEvent.click(screen.getByRole('button', { name: 'Make an episode' }))

    expect(await screen.findByRole('heading', { name: 'Episode' })).toBeDefined()
    const created = calls.find((call) => call.method === 'POST' && call.url.endsWith('/v1/episodes'))
    expect(created?.body).toMatchObject({ max_duration_ms: 20 * 60_000 })
  })

  it('shows every claim beside the source span it cites', async () => {
    mockApi()
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: 'Backlog' }))
    await screen.findByText('Acme raises $20M Series A')
    fireEvent.click(screen.getByRole('button', { name: 'Make an episode' }))
    await screen.findByRole('heading', { name: 'Episode' })

    // Invariant 3, as a user can see it: the spoken sentence and the verbatim source text
    // it is answerable to, in the same row.
    expect(screen.getByRole('columnheader', { name: 'Spoken' })).toBeDefined()
    expect(screen.getByRole('columnheader', { name: 'Source span' })).toBeDefined()
    expect(screen.getByText('Acme raised twenty million dollars.')).toBeDefined()
    expect(screen.getByText('Acme raises $20M Series A', { selector: 'blockquote' })).toBeDefined()
    expect(screen.getByText(/chars 0–25/)).toBeDefined()
  })

  it('offers the private feed URL rather than an in-page player', async () => {
    mockApi()
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: 'Backlog' }))
    await screen.findByText('Acme raises $20M Series A')
    fireEvent.click(screen.getByRole('button', { name: 'Make an episode' }))

    expect(await screen.findByText('https://example.test/feed.xml?token=secret')).toBeDefined()
    // Phase 1 deliberately ships RSS instead of a player: a browser has no background
    // audio and no offline, and a dog walk needs both.
    expect(document.querySelector('audio')).toBeNull()
  })
})

describe('connecting a mailbox', () => {
  it('lists sources and reads a pending one as waiting, not as failed', async () => {
    mockApi()
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: 'Sources' }))

    expect(await screen.findByText('Gmail')).toBeDefined()
    expect(screen.getByText(/gmail . waiting for consent/)).toBeDefined()
  })

  it('offers Gmail and nothing else', async () => {
    // The API answers 400 for any other provider, because X bookmarks are not built. A
    // button for one would be a promise the backend refuses to keep.
    mockApi()
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: 'Sources' }))
    await screen.findByText('Gmail')

    expect(screen.getByRole('button', { name: 'Connect Gmail' })).toBeDefined()
    expect(screen.queryByRole('button', { name: /Connect X/ })).toBeNull()
  })

  it('starts consent with the redirect URI this origin will come back on', async () => {
    const calls = mockApi({
      '/v1/sources/connect': {
        source_id: 'src_2',
        authorization_url: 'https://accounts.google.test/o/oauth2/v2/auth?client_id=x',
        state: 'st_1',
      },
    })
    const navigate = vi.fn()
    render(<Sources navigate={navigate} />)
    await screen.findByText('Gmail')

    fireEvent.change(screen.getByLabelText('Gmail search (optional)'), {
      target: { value: 'from:newsletter@example.test' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Connect Gmail' }))

    await waitFor(() => expect(navigate).toHaveBeenCalled())
    const connect = calls.find((call) => call.url.includes('/v1/sources/connect'))
    expect(connect?.method).toBe('POST')
    expect(connect?.body).toEqual({
      provider: 'gmail',
      name: 'Gmail',
      query: 'from:newsletter@example.test',
      // Registered on the OAuth client, and matched by Google as an exact string.
      redirect_uri: `${window.location.origin}/oauth/callback`,
    })
    expect(navigate).toHaveBeenCalledWith(
      'https://accounts.google.test/o/oauth2/v2/auth?client_id=x',
    )
    // Remembered before the redirect: after it, nothing in this tab gets to run.
    expect(window.sessionStorage.getItem('motet.oauthState')).toBe('st_1')
  })

  it('sends a blank query as null, which is what asks for the provider default', async () => {
    const calls = mockApi({
      '/v1/sources/connect': {
        source_id: 'src_2',
        authorization_url: 'https://accounts.google.test/',
        state: 'st_2',
      },
    })
    render(<Sources navigate={vi.fn()} />)
    await screen.findByText('Gmail')

    fireEvent.click(screen.getByRole('button', { name: 'Connect Gmail' }))

    await waitFor(() => {
      const connect = calls.find((call) => call.url.includes('/v1/sources/connect'))
      expect(connect?.body).toMatchObject({ query: null })
    })
  })

  it('shows the API own message when the OAuth client is not provisioned', async () => {
    // The dormant case today: real mode with no Google OAuth client provisioned, which
    // the API answers 503 to while naming the variable that is missing. That message is
    // worth more than anything this screen could invent, so it is the one shown.
    //
    // Its own stub rather than mockApi: that helper answers by URL prefix and has no way
    // to express a failure.
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const failing = String(input).includes('/v1/sources/connect')
        return {
          ok: !failing,
          status: failing ? 503 : 200,
          statusText: 'Service Unavailable',
          json: async () =>
            failing ? { detail: 'GOOGLE_OAUTH_CLIENT_ID is not set.' } : [GMAIL_SOURCE],
        } as Response
      }),
    )
    render(<Sources navigate={vi.fn()} />)
    await screen.findByText('Gmail')

    fireEvent.click(screen.getByRole('button', { name: 'Connect Gmail' }))

    expect(await screen.findByText(/GOOGLE_OAUTH_CLIENT_ID is not set/)).toBeDefined()
  })
})

describe('the /oauth/callback landing', () => {
  it('exchanges the code and reports the mailbox connected', async () => {
    const connected: Source = { ...GMAIL_SOURCE, active: true, connected: true }
    const calls = mockApi({ '/v1/sources/callback': connected })
    window.sessionStorage.setItem('motet.oauthState', 'st_1')
    window.history.replaceState({}, '', '/oauth/callback?code=abc123&state=st_1')

    render(<App />)

    expect(await screen.findByText(/Gmail is connected/)).toBeDefined()
    const callback = calls.find((call) => call.url.includes('/v1/sources/callback'))
    expect(callback?.method).toBe('POST')
    expect(callback?.body).toEqual({ state: 'st_1', code: 'abc123' })
  })

  it('exchanges once, and clears the code out of the address bar', async () => {
    // StrictMode runs effects twice and an authorization code is single-use, so a second
    // exchange would overwrite a success with "already used"; a reload of a URL still
    // carrying the code would do the same.
    const calls = mockApi({ '/v1/sources/callback': { ...GMAIL_SOURCE, connected: true } })
    window.history.replaceState({}, '', '/oauth/callback?code=abc123&state=st_1')

    render(
      <StrictMode>
        <App />
      </StrictMode>,
    )

    await screen.findByText(/is connected/)
    expect(calls.filter((call) => call.url.includes('/v1/sources/callback'))).toHaveLength(1)
    expect(window.location.pathname).toBe('/')
  })

  it('treats a denied consent as an answer, not as a crash', async () => {
    const calls = mockApi()
    window.history.replaceState({}, '', '/oauth/callback?error=access_denied&state=st_1')

    render(<App />)

    expect(await screen.findByText(/did not grant access/)).toBeDefined()
    expect(calls.find((call) => call.url.includes('/v1/sources/callback'))).toBeUndefined()
  })

  it('refuses a callback belonging to a different authorization', async () => {
    const calls = mockApi()
    window.sessionStorage.setItem('motet.oauthState', 'st_1')
    window.history.replaceState({}, '', '/oauth/callback?code=abc123&state=st_ELSEWHERE')

    render(<App />)

    expect(await screen.findByText(/different authorization/)).toBeDefined()
    expect(calls.find((call) => call.url.includes('/v1/sources/callback'))).toBeUndefined()
  })

  it('hands the user back to the normal UI when it is done', async () => {
    mockApi({ '/v1/sources/callback': { ...GMAIL_SOURCE, connected: true, active: true } })
    window.history.replaceState({}, '', '/oauth/callback?code=abc123&state=st_1')
    render(<App />)
    await screen.findByText(/is connected/)

    fireEvent.click(screen.getByRole('button', { name: 'Back to Motet' }))

    expect(await screen.findByRole('heading', { name: 'Sources' })).toBeDefined()
    expect(screen.getByRole('navigation', { name: 'Screens' })).toBeDefined()
  })
})

describe('the generated contract', () => {
  it('types /internal/health off openapi.yaml', () => {
    // Compile-time assertion: if the API drops a field, `bin/ci` regenerates
    // schema.gen.ts, this stops type-checking, and the drift is caught here rather
    // than in a browser.
    const health: HealthResponse = {
      status: 'ok',
      service: 'motet-api',
      telemetry_configured: false,
      errors_configured: false,
      authenticated: true,
      inference_mode: 'fake',
    }
    expect(health.status).toBe('ok')
  })
})
