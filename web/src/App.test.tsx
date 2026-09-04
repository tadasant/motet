import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App'
import type {
  Episode,
  HealthResponse,
  IngestionItem,
  NewsItem,
  SessionInfo,
  Source,
} from './api/client'
// Imported directly for the connect tests: handing the browser to Google is the one line
// of that flow jsdom cannot execute, and the screen takes it as a prop for that reason.
import { SignIn } from './screens/SignIn'
import { Sources } from './screens/Sources'

const NEWS_ITEM: NewsItem = {
  id: 'ni_1',
  title: 'Acme raises $20M Series A',
  summary: 'Acme announced the round on Tuesday.',
  source_item_ids: ['si_1', 'si_2'],
  read: false,
  created_at: '2026-08-24T00:00:00Z',
}

/** A paste the queue has accepted and not yet picked up. */
const QUEUED: IngestionItem = {
  id: 'si_9',
  title: 'Newsletter I just pasted',
  state: 'pending',
  attempts: 0,
  max_attempts: 5,
  next_attempt_at: '2026-08-24T00:00:05Z',
  last_error: null,
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

const SESSION: SessionInfo = {
  how: 'session',
  email: 'owner@motet.test',
  expires_at: '2026-09-23T00:00:00Z',
  login_configured: true,
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

/**
 * The built-in source, exactly as `GET /v1/sources` reports it: **active**, and
 * `connected: false` forever, because there is no credential for it to hold.
 */
const PASTE_SOURCE: Source = {
  id: 'src_paste',
  kind: 'paste',
  name: 'Pasted text',
  active: true,
  connected: false,
  scopes: [],
  last_polled_at: null,
  last_error: null,
  created_at: '2026-08-24T00:00:00Z',
}

/**
 * Route a fake fetch by URL, so a test asserts on what the SPA actually requested.
 *
 * A key may be prefixed with a method — `'GET /v1/episodes'` — and those are matched
 * first, and **exactly**. `/v1/episodes` is the one path where GET and POST mean
 * genuinely different things: the list, and making a new one. Without the distinction the
 * list route served a single episode object, which typechecks nowhere and is not what the
 * API does — and without the *exact* match, `GET /v1/episodes/ep_1` would be served the
 * list, so the polling path the episode screen runs on would be tested against a shape it
 * never sees.
 */
function mockApi(overrides: Record<string, unknown> = {}) {
  const calls: { url: string; method: string; body: unknown }[] = []
  const routes: Record<string, unknown> = {
    '/v1/news-items': [NEWS_ITEM],
    '/v1/ingestion': [],
    '/v1/processing': {
      now: '2026-08-24T00:00:10Z',
      worker_last_seen_at: null,
      queues: [],
    },
    '/v1/feed': { url: 'https://example.test/feed.xml?token=secret', token: 'secret' },
    'GET /v1/episodes': [EPISODE],
    '/v1/episodes': EPISODE,
    // Both, because every real response carries `src_paste` — migration 0002 seeds it —
    // and a fixture that left it out is part of why motet#39 survived.
    '/v1/sources': [PASTE_SOURCE, GMAIL_SOURCE],
    '/v1/auth/session': SESSION,
    ...overrides,
  }
  // An override of `undefined` removes the route rather than serving undefined for it,
  // which is how a test says "this API does not have that endpoint" and gets a real 404.
  for (const [route, value] of Object.entries(routes)) {
    if (value === undefined) delete routes[route]
  }
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    calls.push({
      url,
      method,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    })
    const longestFirst = (names: string[]) => names.sort((a, b) => b.length - a.length)
    const names = Object.keys(routes)
    const key =
      names.filter((route) => route.includes(' ')).find((route) => `${method} ${url}` === route) ??
      longestFirst(names.filter((route) => !route.includes(' '))).find((route) =>
        url.startsWith(route),
      )
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
  // A browser holding no token now sees the sign-in door instead of the tab strip, which
  // is the point of Google Sign-In. Every test below is about what a *signed-in* browser
  // does, so they start with a credential in the slot; the door has its own describe.
  window.localStorage.setItem('motet.apiToken', 'test-token')
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

  it('shows a pasted item that is still queued, rather than losing it', async () => {
    // The defect this replaces: the paste was accepted, the confirmation said "pending",
    // and then there was nowhere at all it could be seen again.
    mockApi({ '/v1/ingestion': [QUEUED] })
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: /Backlog/ }))

    expect(await screen.findByRole('heading', { name: 'Processing' })).toBeDefined()
    expect(screen.getByText('Newsletter I just pasted')).toBeDefined()
    expect(screen.getByText('Queued')).toBeDefined()
  })

  it('tells a retrying item apart from a stuck one, and says why for both', async () => {
    mockApi({
      '/v1/ingestion': [
        {
          ...QUEUED,
          id: 'si_retry',
          title: 'Still going',
          attempts: 3,
          last_error: 'ReasoningNotAppliedError: no reasoning evidence in the response',
        },
        {
          ...QUEUED,
          id: 'si_dead',
          title: 'Gave up',
          state: 'failed',
          attempts: 5,
          next_attempt_at: null,
          last_error: 'OpenRouter refused: 402 insufficient credits',
        },
      ],
    })
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: /Backlog/ }))

    await screen.findByRole('heading', { name: 'Processing' })
    // An item on its fourth attempt and an item nobody will ever try again are not the
    // same thing to someone standing there waiting, and one spinner for both says neither.
    expect(screen.getByText('Retrying')).toBeDefined()
    expect(screen.getByText('Failed')).toBeDefined()
    expect(screen.getByText(/Attempt 3 of 5 failed/)).toBeDefined()
    expect(screen.getByText(/Gave up after 5 attempts/)).toBeDefined()
    // Counted as what each of them is, rather than rolled into one "in flight" number.
    expect(screen.getByText('1 on the way in, 1 stuck.')).toBeDefined()
    // And the reason, verbatim — enough to decide whether to wait, re-paste, or report it.
    expect(screen.getByText(/ReasoningNotAppliedError/)).toBeDefined()
    expect(screen.getByText(/402 insufficient credits/)).toBeDefined()
  })

  it('keeps the backlog when the ingestion route cannot answer, and says so', async () => {
    // The two lists come from one API but the SPA and the API are separate services. A
    // failure of the secondary panel must not take the primary list down with it — and
    // must not be reported as "nothing is being processed", which is a different claim.
    mockApi({ '/v1/ingestion': undefined })
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: /Backlog/ }))

    expect(await screen.findByText('Acme raises $20M Series A')).toBeDefined()
    expect(screen.getByText(/Could not check what is still being processed/)).toBeDefined()
  })

  it('polls while something is pending, and stops once nothing is', async () => {
    // The riskiest line in the change: a poll that never starts leaves the panel stale,
    // and one that never stops hammers the API from an idle tab forever.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const calls = mockApi({ '/v1/ingestion': [QUEUED] })
      render(<App />)
      await screen.findByRole('heading', { name: 'Paste in' })
      const before = calls.filter((call) => call.url.includes('/v1/ingestion')).length

      await vi.advanceTimersByTimeAsync(7_000)
      const polled = calls.filter((call) => call.url.includes('/v1/ingestion')).length
      expect(polled).toBeGreaterThan(before)

      // Nothing pending any more: the interval must tear itself down rather than run on.
      calls.length = 0
      vi.mocked(fetch).mockClear()
      mockApi({ '/v1/ingestion': [{ ...QUEUED, state: 'integrated' }] })
      await vi.advanceTimersByTimeAsync(4_000)
      const settled = calls.filter((call) => call.url.includes('/v1/ingestion')).length
      await vi.advanceTimersByTimeAsync(10_000)
      expect(calls.filter((call) => call.url.includes('/v1/ingestion')).length).toBe(settled)
    } finally {
      vi.useRealTimers()
    }
  })

  it('counts only what is unsettled on the tab', async () => {
    // A badge stuck at 3 for the ten minutes after everything landed means nothing.
    mockApi({
      '/v1/ingestion': [
        { ...QUEUED, id: 'si_done', state: 'integrated' },
        { ...QUEUED, id: 'si_open' },
      ],
    })
    render(<App />)

    expect(await screen.findByRole('button', { name: 'Backlog 1' })).toBeDefined()
  })

  it('counts what is in flight on the tab, so it is visible from the paste screen', async () => {
    mockApi({ '/v1/ingestion': [QUEUED] })
    render(<App />)

    // Still on Paste in: someone who has just pasted has no reason to go to the backlog
    // unless something there tells them to.
    await screen.findByRole('heading', { name: 'Paste in' })
    expect(await screen.findByRole('button', { name: 'Backlog 1' })).toBeDefined()
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

  it('finds the episode you already have, without anyone opening it first', async () => {
    // motet#44. Nothing loaded episode state on mount, so a reload — the realistic thing
    // to do while a multi-minute pipeline runs — emptied the tab and left a finished
    // episode reachable only through the RSS feed. This test is a fresh page load: no
    // backlog visit, no "Make an episode", straight to the tab.
    mockApi()
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: 'Episode' }))

    expect(await screen.findByText(/Morning briefing/)).toBeDefined()
    expect(screen.queryByText('Make one from the backlog.')).toBeNull()
  })

  it('says so when there is genuinely no episode, and not before it has looked', async () => {
    mockApi({ 'GET /v1/episodes': [] })
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: 'Episode' }))

    expect(await screen.findByText('Make one from the backlog.')).toBeDefined()
  })

  it('does not report a failed episode fetch as having no episodes', async () => {
    // The same distinction the ingestion panel keeps: "you have none" and "I could not
    // find out" are different claims, and the second one wearing the first one's clothes
    // is exactly the disappearance motet#44 is about.
    mockApi({ 'GET /v1/episodes': undefined, '/v1/episodes': undefined })
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: 'Episode' }))

    expect(await screen.findByText(/Could not load your episodes/)).toBeDefined()
    expect(screen.queryByText('Make one from the backlog.')).toBeNull()
  })

  it('keeps asking whether a worker is running while an episode is mid-pipeline', async () => {
    // `processing` is fetched by the backlog refresh, which used to stop the moment
    // nothing was pending in ingestion. On a reload during a render that is immediately,
    // so the episode screen's banner would be computed from a heartbeat frozen at mount
    // and would turn red on its own a few minutes later.
    const pending = { ...EPISODE, state: 'rendering', segments: [] }
    const calls = mockApi({ 'GET /v1/episodes': [pending], '/v1/episodes': pending })
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: 'Episode' }))
    await screen.findByText(/Working…/)

    const before = calls.filter((call) => call.url.startsWith('/v1/processing')).length
    await waitFor(
      () =>
        expect(
          calls.filter((call) => call.url.startsWith('/v1/processing')).length,
        ).toBeGreaterThan(before),
      { timeout: 6000 },
    )
  })

  it('offers every other episode, so the second-newest is reachable too', async () => {
    const older = { ...EPISODE, id: 'ep_0', title: 'Yesterday briefing' }
    mockApi({ 'GET /v1/episodes': [EPISODE, older] })
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: 'Episode' }))
    await screen.findByText(/Morning briefing/)

    fireEvent.click(await screen.findByRole('button', { name: 'Yesterday briefing' }))
    // The heading line, not the picker entry it was chosen from: `strong` is the title
    // of the episode on screen, and the picker only ever lists the *other* ones.
    expect(await screen.findByText('Yesterday briefing', { selector: 'strong' })).toBeDefined()
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
    // The other half of the row, asserted here because the paste case below asserts its
    // *absence*: a poll line suppressed for everything would satisfy that one on its own.
    expect(screen.getByText(/Never polled/)).toBeDefined()
  })

  it('gives a polled source its poll time and its scopes on one line', async () => {
    const connected: Source = {
      ...GMAIL_SOURCE,
      active: true,
      connected: true,
      scopes: ['gmail.readonly'],
      last_polled_at: '2026-08-24T00:00:00Z',
    }
    mockApi({ '/v1/sources': [connected] })
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: 'Sources' }))

    expect(await screen.findByText(/gmail . connected/)).toBeDefined()
    expect(screen.getByText(/^Last polled .* . gmail.readonly$/)).toBeDefined()
  })

  it('reads the paste source off `active`, because consent and polling do not apply to it', async () => {
    // motet#39. `statusOf` branched on `connected` alone, so the one source that can
    // never connect — and the only one actually in use — was labelled an
    // abandoned OAuth attempt, directly under copy saying pasting in needs nothing.
    mockApi({ '/v1/sources': [PASTE_SOURCE] })
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: 'Sources' }))

    expect(await screen.findByText('Pasted text')).toBeDefined()
    expect(screen.getByText(/paste . ready/)).toBeDefined()
    // The `paste ·` prefix is what keeps this off the panel below, which quotes the
    // phrase — correctly, because that paragraph is about mailboxes.
    expect(screen.queryByText(/paste . waiting for consent/)).toBeNull()
    // Nothing polls pasted text, so a poll time is not missing — it is inapplicable, and
    // "Never polled" reads as a fetch that has never fired.
    expect(screen.queryByText(/Never polled/)).toBeNull()
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

  it('says what a failed fetch means instead of showing the browser string', async () => {
    // The bug as the user met it. A rejected `fetch` means the request never completed
    // at the network layer, and the browser tells JavaScript nothing about why — the
    // same `TypeError: Failed to fetch` covers a refused cross-origin response, DNS,
    // TLS, being offline, and a server that closed the connection. That bare string was
    // once the entire report of a broken Gmail connect. It now arrives naming the URL
    // and saying that no answer came back from Motet at all.
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).includes('/v1/sources/connect')) {
          throw new TypeError('Failed to fetch')
        }
        return { ok: true, status: 200, json: async () => [GMAIL_SOURCE] } as Response
      }),
    )
    render(<Sources navigate={vi.fn()} />)
    await screen.findByText('Gmail')

    fireEvent.click(screen.getByRole('button', { name: 'Connect Gmail' }))

    const shown = await screen.findByText(/never completed/)
    expect(shown.textContent).toContain('/v1/sources/connect')
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

describe('signing in', () => {
  it('shows the door, not the app, when this browser holds nothing', async () => {
    // The whole point. What used to be "open the disclosure and paste MOTET_API_TOKEN" is
    // now a button, and a phone on a dog walk is where that difference is felt.
    window.localStorage.clear()
    mockApi()
    render(<App />)

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeDefined()
    expect(screen.getByRole('button', { name: 'Sign in with Google' })).toBeDefined()
    expect(screen.queryByRole('navigation', { name: 'Screens' })).toBeNull()
  })

  it('keeps the API token as a way in, because the feed and every script still use it', () => {
    // Not a fallback out of politeness: the bearer path is not being replaced, it is
    // being demoted out of a human's hands.
    window.localStorage.clear()
    mockApi()
    render(<App />)

    expect(screen.getByLabelText('API token')).toBeDefined()
  })

  it('starts a sign-in against this origin own callback URL', async () => {
    const calls = mockApi({
      '/v1/auth/google/start': {
        authorization_url: 'https://accounts.google.test/o/oauth2/v2/auth?client_id=x',
        state: 'login.st_1',
      },
    })
    const navigate = vi.fn()
    render(<SignIn navigate={navigate} />)

    fireEvent.click(screen.getByRole('button', { name: 'Sign in with Google' }))

    await waitFor(() => expect(navigate).toHaveBeenCalled())
    const started = calls.find((call) => call.url.includes('/v1/auth/google/start'))
    expect(started?.method).toBe('POST')
    // Registered on the OAuth client, and matched by Google as an exact string.
    expect(started?.body).toEqual({ redirect_uri: `${window.location.origin}/oauth/callback` })
    // Remembered before the redirect: after it, nothing in this tab gets to run.
    expect(window.sessionStorage.getItem('motet.oauthState')).toBe('login.st_1')
  })

  it('shows the API own message when sign-in is not configured', async () => {
    // The fail-closed case: no allowlist, so the API refuses before sending anyone to
    // Google only to deny them on the way back. Its sentence names the variable.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 503,
        statusText: 'Service Unavailable',
        json: async () => ({ detail: 'MOTET_ALLOWED_EMAILS is unset.' }),
      })) as unknown as typeof fetch,
    )
    render(<SignIn navigate={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: 'Sign in with Google' }))

    expect(await screen.findByText(/MOTET_ALLOWED_EMAILS is unset/)).toBeDefined()
  })
})

describe('the /oauth/callback landing, for a sign-in', () => {
  it('exchanges the code and puts the session token in the slot the API token used', async () => {
    // The property that keeps every other call site unchanged: a session token is just a
    // bearer token, so it goes where the bearer token goes.
    window.localStorage.clear()
    const calls = mockApi({
      '/v1/auth/google/callback': {
        token: 'sess_abc',
        email: 'owner@motet.test',
        expires_at: '2026-09-23T00:00:00Z',
      },
    })
    window.sessionStorage.setItem('motet.oauthState', 'login.st_1')
    window.history.replaceState({}, '', '/oauth/callback?code=abc123&state=login.st_1')

    render(<App />)

    expect(await screen.findByText(/Signed in as owner@motet.test/)).toBeDefined()
    const callback = calls.find((call) => call.url.includes('/v1/auth/google/callback'))
    expect(callback?.body).toEqual({ state: 'login.st_1', code: 'abc123' })
    expect(window.localStorage.getItem('motet.apiToken')).toBe('sess_abc')
  })

  it('tells a sign-in callback from a mailbox one by its state, and nothing else', async () => {
    // Both flows land on this one path. `state` is the only value that survives the round
    // trip through Google, so it is the only thing that can say which finished.
    const calls = mockApi({ '/v1/sources/callback': { ...GMAIL_SOURCE, connected: true } })
    window.history.replaceState({}, '', '/oauth/callback?code=abc123&state=st_mailbox')

    render(<App />)

    await screen.findByText(/is connected/)
    expect(calls.find((call) => call.url.includes('/v1/auth/google/callback'))).toBeUndefined()
  })

  it('exchanges once, and clears the code out of the address bar', async () => {
    // StrictMode double-invokes effects and an authorization code is single-use.
    const calls = mockApi({
      '/v1/auth/google/callback': {
        token: 'sess_abc',
        email: 'owner@motet.test',
        expires_at: '2026-09-23T00:00:00Z',
      },
    })
    window.history.replaceState({}, '', '/oauth/callback?code=abc123&state=login.st_1')

    render(
      <StrictMode>
        <App />
      </StrictMode>,
    )

    await screen.findByText(/Signed in as/)
    expect(calls.filter((call) => call.url.includes('/v1/auth/google/callback'))).toHaveLength(1)
    expect(window.location.pathname).toBe('/')
  })

  it('reads a refused sign-in as an answer, not as a crash', async () => {
    const calls = mockApi()
    window.history.replaceState({}, '', '/oauth/callback?error=access_denied&state=login.st_1')

    render(<App />)

    expect(await screen.findByText(/did not finish signing in/)).toBeDefined()
    expect(calls.find((call) => call.url.includes('/v1/auth/google/callback'))).toBeUndefined()
  })

  it('shows the API refusal when the account is not on the allowlist', async () => {
    // The case this whole design exists for: the consent screen is open to the internet,
    // so a stranger can arrive here having genuinely signed in to Google. The API is what
    // says no, and it says it in a sentence worth showing.
    window.localStorage.clear()
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 403,
        statusText: 'Forbidden',
        json: async () => ({ detail: 'That Google account is not allowed to use this Motet.' }),
      })) as unknown as typeof fetch,
    )
    window.history.replaceState({}, '', '/oauth/callback?code=abc123&state=login.st_1')

    render(<App />)

    expect(await screen.findByText(/not allowed to use this Motet/)).toBeDefined()
    expect(window.localStorage.getItem('motet.apiToken')).toBeNull()
  })
})

describe('a session that stops working', () => {
  it('drops the dead token and shows the door again', async () => {
    // A session expires after 30 days and can be revoked from another device. Without
    // this the SPA keeps a dead string in storage, renders a tab strip whose every screen
    // 401s, and offers no way back except realising that emptying the *API token* field
    // is what signs you out.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 401,
        statusText: 'Unauthorized',
        json: async () => ({ detail: 'This session is no longer allowed. Sign in again.' }),
      })) as unknown as typeof fetch,
    )

    render(<App />)

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeDefined()
    expect(window.localStorage.getItem('motet.apiToken')).toBeNull()
  })

  it('shows the app on an unlocked deployment, which has no door to pass', async () => {
    // MOTET_API_TOKEN unset is the documented local setup: the API answers everything.
    // A browser cannot tell "I have no credential" from "no credential is needed" without
    // asking, and a sign-in screen in front of an open API is a dead end — the button
    // 503s, because a laptop has no allowlist either.
    window.localStorage.clear()
    mockApi({
      '/v1/auth/session': { how: 'open', email: null, expires_at: null, login_configured: false },
    })

    render(<App />)

    expect(await screen.findByRole('heading', { name: 'Paste in' })).toBeDefined()
    expect(screen.getByRole('navigation', { name: 'Screens' })).toBeDefined()
  })
})

describe('signing out', () => {
  it('says who is signed in and revokes the session', async () => {
    const calls = mockApi({ '/v1/auth/logout': {} })
    render(<App />)
    await screen.findByRole('button', { name: 'Sign out' })
    expect(screen.getByText(/owner@motet.test/)).toBeDefined()

    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeDefined()
    expect(calls.find((call) => call.url.includes('/v1/auth/logout'))?.method).toBe('POST')
    expect(window.localStorage.getItem('motet.apiToken')).toBeNull()
  })

  it('says nothing about a session when the caller is using the shared token', async () => {
    // `how: 'token'` carries no address, because the shared secret belongs to no person.
    mockApi({ '/v1/auth/session': { how: 'token', email: null, expires_at: null, login_configured: true } })
    render(<App />)

    await screen.findByRole('heading', { name: 'Paste in' })
    expect(screen.queryByRole('button', { name: 'Sign out' })).toBeNull()
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
      telemetry_exporting: false,
      errors_configured: false,
      authenticated: true,
      login_configured: true,
      vault_backend: 'kms',
      vault_ready: true,
      inference_mode: 'fake',
    }
    expect(health.status).toBe('ok')
  })
})
