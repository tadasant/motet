import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App'
import type { Episode, HealthResponse, NewsItem } from './api/client'

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

/** Route a fake fetch by URL, so a test asserts on what the SPA actually requested. */
function mockApi(overrides: Record<string, unknown> = {}) {
  const calls: { url: string; method: string; body: unknown }[] = []
  const routes: Record<string, unknown> = {
    '/v1/news-items': [NEWS_ITEM],
    '/v1/feed': { url: 'https://example.test/feed.xml?token=secret', token: 'secret' },
    '/v1/episodes': EPISODE,
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
})

afterEach(() => {
  vi.unstubAllGlobals()
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

describe('the generated contract', () => {
  it('types /healthz off openapi.yaml', () => {
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
