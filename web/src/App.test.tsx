import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App'
import type {
  AdminOverview,
  Episode,
  HealthResponse,
  HeldSourceItem,
  IngestionItem,
  NewsItem,
  SessionInfo,
  Source,
} from './api/client'
// Imported directly for the sign-in tests: handing the browser to Google is the one line
// of that flow jsdom cannot execute, and the screen takes it as a prop for that reason.
import { SignIn } from './screens/SignIn'

const NEWS_ITEM: NewsItem = {
  id: 'ni_1',
  title: 'Acme raises $20M Series A',
  summary: 'Acme announced the round on Tuesday.',
  source_item_ids: ['si_1', 'si_2'],
  sources: [
    { id: 'si_1', title: 'Acme newsletter' },
    { id: 'si_2', title: 'Acme, again' },
  ],
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
  source_kind: 'paste',
  source_id: 'src_paste',
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
  listened_through_ms: 0,
  keep_in_backlog: false,
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
          start_ms: 0,
          duration_ms: 92_000,
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
  admin: false,
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
  // Polled with the default search until it has one of its own; no poll has run yet.
  query: 'category:updates OR category:promotions',
  first_sync_days: null,
  last_sync: null,
  disconnected_at: null,
  items_pulled_in: 0,
  items_integrated: 0,
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
  // Nothing polls it, so it has no search and no sync to report.
  query: null,
  first_sync_days: null,
  last_sync: null,
  disconnected_at: null,
  items_pulled_in: 0,
  items_integrated: 0,
}

/** A held source item, as `/v1/source-items/held` reports one (motet#91). */
const HELD_ITEM: HeldSourceItem = {
  id: 'si_held_1',
  title: 'Weekly wire',
  source_id: 'src_gmail',
  source_kind: 'gmail',
  source_name: 'Newsletters',
  received_at: '2026-08-19T10:30:00Z',
  chars: 4_200,
  preview: 'This week in widgets.',
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
      readiness: [],
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
  it('shows every section and starts on the backlog', async () => {
    mockApi()
    render(<App />)
    for (const label of ['Backlog', 'Episodes', 'Sources', 'Paste in']) {
      expect(screen.getByRole('link', { name: label })).toBeDefined()
    }
    expect(await screen.findByRole('heading', { name: 'Backlog', level: 1 })).toBeDefined()
  })

  it('sends the bearer token it was given', async () => {
    window.localStorage.setItem('motet.apiToken', 'shhh')
    mockApi()
    render(<App />)
    await screen.findByRole('heading', { name: 'Backlog', level: 1 })
    const fetchMock = vi.mocked(fetch)
    const [, init] = fetchMock.mock.calls[0]!
    expect((init as RequestInit).headers).toMatchObject({ Authorization: 'Bearer shhh' })
  })

  it('posts pasted text to the ingestion route', async () => {
    const calls = mockApi({ '/v1/sources/paste': { id: 'si_9', title: 'T', state: 'pending' } })
    render(<App />)
    fireEvent.click(screen.getByRole('link', { name: 'Paste in' }))
    await screen.findByRole('heading', { name: 'Paste in', level: 1 })

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
    fireEvent.click(screen.getByRole('link', { name: 'Backlog' }))

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
    fireEvent.click(screen.getByRole('link', { name: /Backlog/ }))

    expect(await screen.findByRole('heading', { name: 'Processing' })).toBeDefined()
    expect(screen.getByText('Newsletter I just pasted')).toBeDefined()
    expect(screen.getByText('Queued')).toBeDefined()
  })

  it('tells a retrying item apart from a failed one, and says why for both', async () => {
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
    fireEvent.click(screen.getByRole('link', { name: /Backlog/ }))

    await screen.findByRole('heading', { name: 'Processing' })
    // An item on its fourth attempt and an item nobody will ever try again are not the
    // same thing to someone standing there waiting, and one spinner for both says neither.
    expect(screen.getByText('Retrying')).toBeDefined()
    expect(screen.getByText('Failed')).toBeDefined()
    expect(screen.getByText(/Attempt 3 of 5 failed/)).toBeDefined()
    expect(screen.getByText(/Gave up after 5 attempts/)).toBeDefined()
    // Counted as what each of them is, rather than rolled into one "in flight" number.
    expect(screen.getByText('1 processing, 1 failed.')).toBeDefined()
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
    fireEvent.click(screen.getByRole('link', { name: /Backlog/ }))

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
      await screen.findByRole('heading', { name: 'Backlog', level: 1 })
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

  it('counts what needs you on the sidebar: held and failed, not in flight', async () => {
    // motet#98. A held item waits for somebody to press Ingest now, and a failed one will
    // never move again; an item a worker is carrying needs nobody. A settled one is not
    // counted either — a badge stuck at 3 after everything landed means nothing.
    mockApi({
      '/v1/ingestion': [
        { ...QUEUED, id: 'si_done', state: 'integrated' },
        { ...QUEUED, id: 'si_open' },
        { ...QUEUED, id: 'si_failed', state: 'failed', attempts: 5 },
      ],
      '/v1/source-items/held': [HELD_ITEM, { ...HELD_ITEM, id: 'si_held_2' }],
    })
    render(<App />)

    const link = await screen.findByRole('link', { name: 'Backlog 3' })
    // Loud, because one of the three is never coming back.
    expect(link.querySelector('.tab-count.failed')).not.toBeNull()
  })

  it('counts held items on the sidebar from another section', async () => {
    mockApi({ '/v1/source-items/held': [HELD_ITEM] })
    window.history.replaceState({}, '', '/paste')
    render(<App />)

    // Still on Paste in: nothing on this screen says a mailbox has pulled anything in, so
    // the badge is the only thing that tells somebody to go and pick.
    await screen.findByRole('heading', { name: 'Paste in', level: 1 })
    expect(await screen.findByRole('link', { name: 'Backlog 1' })).toBeDefined()
  })

  it('does not count an item a worker is still carrying', async () => {
    mockApi({ '/v1/ingestion': [QUEUED] })
    render(<App />)

    await screen.findByText('1 processing.')
    expect(screen.getByRole('link', { name: 'Backlog' })).toBeDefined()
  })

  it('creates an episode from the backlog and opens it', async () => {
    const calls = mockApi()
    render(<App />)
    fireEvent.click(screen.getByRole('link', { name: 'Backlog' }))
    await screen.findByText('Acme raises $20M Series A')

    fireEvent.click(screen.getByRole('button', { name: 'Make an episode' }))

    expect(await screen.findByRole('region', { name: 'Episode' })).toBeDefined()
    expect(window.location.pathname).toBe('/episodes')
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
    fireEvent.click(screen.getByRole('link', { name: 'Episodes' }))

    expect(await screen.findByText(/Morning briefing/)).toBeDefined()
    expect(screen.queryByText('Make one from the backlog.')).toBeNull()
  })

  it('says so when there is genuinely no episode, and not before it has looked', async () => {
    mockApi({ 'GET /v1/episodes': [] })
    render(<App />)
    fireEvent.click(screen.getByRole('link', { name: 'Episodes' }))

    expect(await screen.findByText('Make one from the backlog.')).toBeDefined()
  })

  it('does not report a failed episode fetch as having no episodes', async () => {
    // The same distinction the ingestion panel keeps: "you have none" and "I could not
    // find out" are different claims, and the second one wearing the first one's clothes
    // is exactly the disappearance motet#44 is about.
    mockApi({ 'GET /v1/episodes': undefined, '/v1/episodes': undefined })
    render(<App />)
    fireEvent.click(screen.getByRole('link', { name: 'Episodes' }))

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
    fireEvent.click(screen.getByRole('link', { name: 'Episodes' }))
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

  it('lands on a shelf of every episode, and opens any of them', async () => {
    // motet#89: the section used to *be* one episode's detail, seeded with the newest,
    // and the second-newest was a link in an "Other episodes:" line.
    const older = {
      ...EPISODE,
      id: 'ep_0',
      title: 'Yesterday briefing',
      created_at: '2026-08-23T00:00:00Z',
      published_at: '2026-08-23T00:01:00Z',
    }
    mockApi({ 'GET /v1/episodes': [EPISODE, older] })
    render(<App />)
    fireEvent.click(screen.getByRole('link', { name: 'Episodes' }))

    expect(await screen.findByRole('region', { name: 'Episodes' })).toBeDefined()
    expect(screen.getByRole('button', { name: 'Play Morning briefing' })).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Yesterday briefing' }))
    expect(await screen.findByText('Yesterday briefing', { selector: 'strong' })).toBeDefined()
    // The section's own address throughout: which episode is open is App state, not a path.
    expect(window.location.pathname).toBe('/episodes')

    fireEvent.click(screen.getByRole('button', { name: '← All episodes' }))
    expect(await screen.findByRole('region', { name: 'Episodes' })).toBeDefined()
  })

  it('lands on the detail of an episode still being made — a reload mid-render', async () => {
    // motet#89, question 4: a finished shelf lands on the list, but an episode in the
    // pipeline is almost always the one somebody just asked for, and its Working… copy
    // lives on the detail.
    const rendering = {
      ...EPISODE,
      id: 'ep_2',
      title: 'Fresh briefing',
      state: 'rendering',
      duration_ms: 0,
      created_at: '2026-08-25T00:00:00Z',
      published_at: null,
      segments: [],
    }
    mockApi({ 'GET /v1/episodes': [rendering, EPISODE], '/v1/episodes': rendering })
    window.history.replaceState({}, '', '/episodes')
    render(<App />)

    expect(await screen.findByText('Fresh briefing', { selector: 'strong' })).toBeDefined()
    expect(screen.getByRole('button', { name: '← All episodes' })).toBeDefined()
  })

  it('lands on the shelf when the render finished before the section was first shown', async () => {
    // The landing rule is judged when the section is first shown, not when the first list
    // arrives: a cold load on the backlog mid-render, then a visit once it is done, is a
    // shelf with one more finished episode on it — not a detail nobody asked to open.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const rendering = {
        ...EPISODE,
        id: 'ep_2',
        title: 'Fresh briefing',
        state: 'rendering',
        duration_ms: 0,
        created_at: '2026-08-25T00:00:00Z',
        published_at: null,
      }
      mockApi({ 'GET /v1/episodes': [rendering, EPISODE] })
      window.history.replaceState({}, '', '/backlog')
      render(<App />)
      await screen.findByText('Acme raises $20M Series A')

      mockApi({
        'GET /v1/episodes': [{ ...rendering, state: 'ready', duration_ms: 60_000 }, EPISODE],
      })
      await vi.advanceTimersByTimeAsync(4_000)
      fireEvent.click(screen.getByRole('link', { name: 'Episodes' }))

      expect(await screen.findByRole('region', { name: 'Episodes' })).toBeDefined()
      expect(screen.getByRole('button', { name: 'Play Fresh briefing' })).toBeDefined()
    } finally {
      vi.useRealTimers()
    }
  })

  it('moves a row on Mark listened, and a stale refresh answer does not move it back', async () => {
    // The position is monotonic on the server, so the list the refresh brings back can be
    // older than a write this page has already seen answered. The merge keeps the larger.
    const calls = mockApi({
      '/v1/episodes/ep_1/listened': { episode_id: 'ep_1', news_items_marked_read: 1 },
      '/v1/episodes/ep_1/position': {
        episode_id: 'ep_1',
        listened_through_ms: 92_000,
        news_items_marked_read: 0,
      },
    })
    render(<App />)
    fireEvent.click(screen.getByRole('link', { name: 'Episodes' }))
    await screen.findByRole('region', { name: /Up next/ })
    const lists = () =>
      calls.filter((call) => call.method === 'GET' && call.url.endsWith('/v1/episodes')).length
    const before = lists()

    fireEvent.click(screen.getByRole('button', { name: 'Mark listened' }))

    // Moved on the write's answer, before any refresh has come back...
    expect(await screen.findByRole('heading', { name: 'Listened (1)' })).toBeDefined()
    // ...and still there once the refresh has, with the server's older copy in it.
    await waitFor(() => expect(lists()).toBeGreaterThan(before))
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Listened (1)' })).toBeDefined())
    expect(screen.getByText('All caught up — everything here has been heard.')).toBeDefined()
  })

  it('remembers which episode is open across a visit to another section', async () => {
    // The section unmounts whenever another is showing, so this is App's to remember.
    const older = { ...EPISODE, id: 'ep_0', title: 'Yesterday briefing' }
    mockApi({ 'GET /v1/episodes': [EPISODE, older] })
    render(<App />)
    fireEvent.click(screen.getByRole('link', { name: 'Episodes' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Yesterday briefing' }))
    await screen.findByText('Yesterday briefing', { selector: 'strong' })

    fireEvent.click(screen.getByRole('link', { name: 'Backlog' }))
    await screen.findByText('Acme raises $20M Series A')
    fireEvent.click(screen.getByRole('link', { name: 'Episodes' }))
    expect(await screen.findByText('Yesterday briefing', { selector: 'strong' })).toBeDefined()

    // And the shelf, once it is where the section was left.
    fireEvent.click(screen.getByRole('button', { name: '← All episodes' }))
    fireEvent.click(screen.getByRole('link', { name: 'Paste in' }))
    fireEvent.click(screen.getByRole('link', { name: 'Episodes' }))
    expect(await screen.findByRole('region', { name: 'Episodes' })).toBeDefined()
  })

  it('moves the shelf on the refresh, and never moves which episode is open', async () => {
    // The list rides App's refresh — the section has no fetch of its own — and the merge
    // is what stops a poll from dragging the screen to a different episode.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const rendering = {
        ...EPISODE,
        id: 'ep_2',
        title: 'Fresh briefing',
        state: 'rendering',
        duration_ms: 0,
        created_at: '2026-08-25T00:00:00Z',
        published_at: null,
      }
      const calls = mockApi({ 'GET /v1/episodes': [rendering, EPISODE] })
      render(<App />)
      fireEvent.click(screen.getByRole('link', { name: 'Episodes' }))
      fireEvent.click(await screen.findByRole('button', { name: '← All episodes' }))
      fireEvent.click(screen.getByRole('button', { name: 'Morning briefing' }))
      await screen.findByText('Morning briefing', { selector: 'strong' })

      // Polls while the other one is still rendering: the list is re-asked, and the
      // landing rule — which would open the rendering episode — does not fire again.
      const lists = () =>
        calls.filter((call) => call.method === 'GET' && call.url.endsWith('/v1/episodes')).length
      const before = lists()
      await vi.advanceTimersByTimeAsync(7_000)
      expect(lists()).toBeGreaterThan(before)
      expect(screen.getByText('Morning briefing', { selector: 'strong' })).toBeDefined()

      // Then the render finishes between two polls.
      const later = mockApi({
        'GET /v1/episodes': [{ ...rendering, state: 'ready', duration_ms: 60_000 }, EPISODE],
      })
      await vi.advanceTimersByTimeAsync(4_000)
      expect(
        later.filter((call) => call.method === 'GET' && call.url.endsWith('/v1/episodes')).length,
      ).toBeGreaterThan(0)
      // Still on the episode that was open, not on the one that just changed.
      expect(screen.getByText('Morning briefing', { selector: 'strong' })).toBeDefined()

      fireEvent.click(screen.getByRole('button', { name: '← All episodes' }))
      expect(await screen.findByRole('button', { name: 'Play Fresh briefing' })).toBeDefined()
      expect(screen.queryByText('Working…')).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('shows every claim beside the source span it cites', async () => {
    mockApi()
    render(<App />)
    fireEvent.click(screen.getByRole('link', { name: 'Backlog' }))
    await screen.findByText('Acme raises $20M Series A')
    fireEvent.click(screen.getByRole('button', { name: 'Make an episode' }))
    await screen.findByRole('region', { name: 'Episode' })

    // Invariant 3, as a user can see it: the spoken sentence and the verbatim source text
    // it is answerable to, in the same row.
    expect(screen.getByRole('columnheader', { name: 'Spoken' })).toBeDefined()
    expect(screen.getByRole('columnheader', { name: 'Source span' })).toBeDefined()
    expect(screen.getByText('Acme raised twenty million dollars.')).toBeDefined()
    expect(screen.getByText('Acme raises $20M Series A', { selector: 'blockquote' })).toBeDefined()
    expect(screen.getByText(/chars 0–25/)).toBeDefined()
  })

  it('offers an in-page player and still the private feed URL for the walk', async () => {
    mockApi()
    render(<App />)
    fireEvent.click(screen.getByRole('link', { name: 'Backlog' }))
    await screen.findByText('Acme raises $20M Series A')
    fireEvent.click(screen.getByRole('button', { name: 'Make an episode' }))

    // motet#89 reverses Phase 1's "no player, RSS instead": the player is for a desk,
    // with the transcript beside it. The feed stays, because a browser tab still has no
    // background audio and no offline, and a dog walk needs both.
    expect(await screen.findByText('https://example.test/feed.xml?token=secret')).toBeDefined()
    const audio = document.querySelector('audio')
    expect(audio?.getAttribute('src')).toBe('/v1/episodes/ep_1/audio?token=secret')
  })
})

// One URL per section, so every screen is reachable, a reload keeps its place, and Back
// does what a browser user expects. Forty lines of pushState, not a router.
describe('the app shell', () => {
  it('puts the section in the address bar, so a reload keeps its place', async () => {
    mockApi()
    render(<App />)
    await screen.findByRole('heading', { name: 'Backlog', level: 1 })
    const before = window.history.length

    fireEvent.click(screen.getByRole('link', { name: 'Sources' }))

    expect(window.location.pathname).toBe('/sources')
    // A history entry per section visited, so Back goes back a section.
    expect(window.history.length).toBe(before + 1)
    expect(await screen.findByRole('heading', { name: 'Sources', level: 1 })).toBeDefined()
    expect(screen.getByRole('link', { name: 'Sources' }).getAttribute('aria-current')).toBe('page')
    expect(screen.getByRole('link', { name: 'Backlog' }).getAttribute('aria-current')).toBeNull()
    // So the back button's list of entries says which section each one is.
    expect(document.title).toBe('Sources · Motet')
  })

  it('follows the back button', async () => {
    mockApi()
    render(<App />)
    await screen.findByRole('heading', { name: 'Backlog', level: 1 })
    fireEvent.click(screen.getByRole('link', { name: 'Sources' }))
    await screen.findByRole('heading', { name: 'Sources', level: 1 })

    window.history.back()

    expect(await screen.findByRole('heading', { name: 'Backlog', level: 1 })).toBeDefined()
    expect(window.location.pathname).toBe('/backlog')
  })

  it('lands on the section the address bar names', async () => {
    mockApi()
    window.history.replaceState({}, '', '/episodes')
    render(<App />)
    expect(await screen.findByRole('heading', { name: 'Episodes', level: 1 })).toBeDefined()
    expect(await screen.findByText(/Morning briefing/)).toBeDefined()
    expect(window.location.pathname).toBe('/episodes')
  })

  it('reads a trailing slash as the same place', async () => {
    mockApi()
    window.history.replaceState({}, '', '/sources/')
    render(<App />)
    expect(await screen.findByRole('heading', { name: 'Sources', level: 1 })).toBeDefined()
    expect(screen.getByRole('link', { name: 'Sources' }).getAttribute('aria-current')).toBe('page')
    await waitFor(() => expect(window.location.pathname).toBe('/sources'))
  })

  it('lands / on the backlog, and says so in the address bar without a history entry', async () => {
    mockApi()
    window.history.replaceState({}, '', '/')
    const before = window.history.length
    render(<App />)

    expect(await screen.findByRole('heading', { name: 'Backlog', level: 1 })).toBeDefined()
    await waitFor(() => expect(window.location.pathname).toBe('/backlog'))
    expect(window.history.length).toBe(before)
  })

  it('lands an unknown path on the backlog rather than a 404', async () => {
    mockApi()
    window.history.replaceState({}, '', '/no/such/place')
    render(<App />)

    expect(await screen.findByRole('heading', { name: 'Backlog', level: 1 })).toBeDefined()
    await waitFor(() => expect(window.location.pathname).toBe('/backlog'))
  })

  it('leaves a modified click to the browser, because the items are real links', async () => {
    mockApi()
    render(<App />)
    await screen.findByRole('heading', { name: 'Backlog', level: 1 })

    const link = screen.getByRole('link', { name: 'Sources' })
    expect(link.getAttribute('href')).toBe('/sources')
    // Listening on window, after React's root listener has had its turn: what matters is
    // whether the app claimed the click. The listener then cancels it itself, because
    // jsdom cannot perform the navigation the browser would.
    let claimedByApp: boolean | undefined
    const observe = (event: MouseEvent) => {
      claimedByApp = event.defaultPrevented
      event.preventDefault()
    }
    window.addEventListener('click', observe)
    try {
      fireEvent.click(link, { ctrlKey: true })
    } finally {
      window.removeEventListener('click', observe)
    }

    expect(claimedByApp).toBe(false)
    expect(window.location.pathname).toBe('/backlog')
    expect(screen.getByRole('heading', { name: 'Backlog', level: 1 })).toBeDefined()
  })

  it('mounts the admin screen inside the shell, titled once', async () => {
    mockApi({ '/v1/auth/session': { ...SESSION, admin: true }, '/v1/admin/overview': undefined })
    window.history.replaceState({}, '', '/admin')
    render(<App />)

    expect(await screen.findByRole('heading', { name: 'Admin', level: 1 })).toBeDefined()
    expect(screen.getByRole('navigation', { name: 'Screens' })).toBeDefined()
    expect((await screen.findByRole('link', { name: 'Admin' })).getAttribute('aria-current')).toBe(
      'page',
    )
    expect(await screen.findByRole('button', { name: 'Pause polling' })).toBeDefined()
    expect(screen.getByRole('region', { name: 'Admin' })).toBeDefined()
    // The page-era toolbar is gone — its title is the top bar's and its way out is the
    // sidebar — and what only this screen has, its own poll, stays.
    expect(screen.getAllByRole('heading', { name: 'Admin' })).toHaveLength(1)
    expect(screen.queryByRole('link', { name: /app/ })).toBeNull()
    expect(screen.getByRole('button', { name: 'Pause polling' })).toBeDefined()
    expect(screen.getByRole('button', { name: 'Refresh now' })).toBeDefined()
    // Its tables are wide, and that is the section's layout rather than the screen's.
    expect(document.querySelector('main')?.className).toBe('wide')
  })

  it('gives every other section the reading width', async () => {
    mockApi()
    render(<App />)
    await screen.findByRole('heading', { name: 'Backlog', level: 1 })
    expect(document.querySelector('main')?.className).toBe('reading')
  })

  it('titles each screen once, in the top bar', async () => {
    mockApi()
    render(<App />)
    await screen.findByText('Acme raises $20M Series A')
    expect(screen.getAllByRole('heading', { name: 'Backlog' })).toHaveLength(1)
    expect(screen.getByRole('region', { name: 'Backlog' })).toBeDefined()
  })

  it('keeps a deep link held behind the door, and opens it once there is a way in', async () => {
    window.localStorage.clear()
    mockApi()
    window.history.replaceState({}, '', '/episodes')
    render(<App />)

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeDefined()
    expect(window.location.pathname).toBe('/episodes')

    fireEvent.change(screen.getByLabelText('API token'), { target: { value: 'test-token' } })
    // Typing is not yet a way in: the door stays up, with the field, until it is submitted.
    expect(screen.getByRole('heading', { name: 'Sign in' })).toBeDefined()
    expect(window.localStorage.getItem('motet.apiToken')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Use this token' }))

    expect(await screen.findByRole('heading', { name: 'Episodes', level: 1 })).toBeDefined()
    expect(window.localStorage.getItem('motet.apiToken')).toBe('test-token')
    expect(window.location.pathname).toBe('/episodes')
  })

  it('opens the menu from a button in the collapsed layout, and closes it on navigating', async () => {
    mockApi()
    render(<App />)
    await screen.findByRole('heading', { name: 'Backlog', level: 1 })

    const menu = screen.getByRole('button', { name: 'Menu' })
    expect(menu.getAttribute('aria-controls')).toBe('sidebar-nav')
    expect(document.getElementById('sidebar-nav')).toBe(screen.getByRole('navigation', { name: 'Screens' }))
    expect(menu.getAttribute('aria-expanded')).toBe('false')

    fireEvent.click(menu)
    expect(menu.getAttribute('aria-expanded')).toBe('true')

    fireEvent.click(screen.getByRole('link', { name: 'Sources' }))
    expect(menu.getAttribute('aria-expanded')).toBe('false')

    // And on a change of section the sidebar did not make: the back button.
    fireEvent.click(menu)
    expect(menu.getAttribute('aria-expanded')).toBe('true')
    window.history.back()
    await screen.findByRole('heading', { name: 'Backlog', level: 1 })
    expect(menu.getAttribute('aria-expanded')).toBe('false')
  })

  it('closes the account menu on Escape and puts focus back on its button', async () => {
    mockApi()
    render(<App />)
    const account = await screen.findByRole('button', { name: /owner@motet.test/ })

    fireEvent.click(account)
    expect(account.getAttribute('aria-expanded')).toBe('true')
    const panel = document.getElementById(account.getAttribute('aria-controls')!)
    expect(panel?.textContent).toContain('Sign out')
    screen.getByLabelText('API token').focus()

    fireEvent.keyDown(document, { key: 'Escape' })

    expect(account.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByRole('button', { name: 'Sign out' })).toBeNull()
    expect(document.activeElement).toBe(account)
  })
})

// The connecting-a-mailbox tests live in screens/Sources.test.tsx, beside the screen they
// drive (motet#90). The callback landing below is still App-level: it replaces the shell.

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
    window.sessionStorage.setItem('motet.oauthState', 'st_1')
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
    window.sessionStorage.setItem('motet.oauthState', 'st_1')
    window.history.replaceState({}, '', '/oauth/callback?error=access_denied&state=st_1')

    render(<App />)

    expect(await screen.findByText(/did not grant access/)).toBeDefined()
    expect(calls.find((call) => call.url.includes('/v1/sources/callback'))).toBeUndefined()
  })

  it('says a cancelled consent once, at the top of Sources, after the callback page', async () => {
    // motet#98. Otherwise the only trace of pressing Cancel is a row reading "waiting for
    // consent" — which is what a live attempt looks like too.
    mockApi()
    window.sessionStorage.setItem('motet.oauthState', 'st_1')
    window.history.replaceState({}, '', '/oauth/callback?error=access_denied&state=st_1')
    render(<App />)

    await screen.findByText(/did not grant access/)
    fireEvent.click(screen.getByRole('button', { name: 'Back to Motet' }))

    const sources = await screen.findByRole('region', { name: 'Sources' })
    expect(window.location.pathname).toBe('/sources')
    // The first status on the screen, above the catalog's own per-row notices.
    const [first] = within(sources).getAllByRole('status')
    expect(first?.textContent).toMatch(/did not grant access/)

    // Once: leaving the section takes it down, and coming back does not bring it back.
    fireEvent.click(screen.getByRole('link', { name: /^Backlog/ }))
    await screen.findByRole('region', { name: 'Backlog' })
    fireEvent.click(screen.getByRole('link', { name: 'Sources' }))
    await screen.findByRole('region', { name: 'Sources' })
    expect(screen.queryByText(/did not grant access/)).toBeNull()
  })

  it('refuses a callback belonging to a different authorization', async () => {
    const calls = mockApi()
    window.sessionStorage.setItem('motet.oauthState', 'st_1')
    window.history.replaceState({}, '', '/oauth/callback?code=abc123&state=st_ELSEWHERE')

    render(<App />)

    expect(await screen.findByText(/different authorization/)).toBeDefined()
    expect(calls.find((call) => call.url.includes('/v1/sources/callback'))).toBeUndefined()
  })

  it('routes a connector state to the connector callback and back to Credentials', async () => {
    // motet#102: the third flow on this path. Sent to the mailbox route it would be refused
    // there, and its single-use state spent for nothing.
    const calls = mockApi({
      '/v1/connectors/oauth/callback': {
        id: 'cn_1',
        kind: 'mcp',
        label: 'Mail (read-only)',
        domain: null,
        domains: [],
        url: 'https://mcp.example/mcp',
        username: null,
        has_secret: true,
        secret_expires_at: null,
        oauth_issuer: 'https://mcp.example',
        oauth_registered: true,
        risk_acknowledged_at: '2026-09-13T00:00:00Z',
        status: 'ready',
        last_error: null,
        created_at: '2026-09-13T00:00:00Z',
        updated_at: '2026-09-13T00:00:00Z',
      },
      '/v1/connectors': [],
    })
    window.sessionStorage.setItem('motet.oauthState', 'connector.st')
    window.history.replaceState(
      {},
      '',
      '/oauth/callback?code=abc123&state=connector.st&iss=https%3A%2F%2Fmcp.example',
    )

    render(<App />)

    expect(await screen.findByText(/Mail \(read-only\) is authorized/)).toBeDefined()
    const exchange = calls.find((call) => call.url.includes('/v1/connectors/oauth/callback'))
    expect(exchange?.body).toEqual({ state: 'connector.st', code: 'abc123', iss: 'https://mcp.example' })
    expect(calls.find((call) => call.url.includes('/v1/sources/callback'))).toBeUndefined()

    fireEvent.click(screen.getByRole('button', { name: 'Back to Credentials' }))
    await screen.findByRole('region', { name: 'Credentials' })
    expect(window.location.pathname).toBe('/credentials')
  })

  it('hands the user back to the normal UI when it is done', async () => {
    mockApi({ '/v1/sources/callback': { ...GMAIL_SOURCE, connected: true, active: true } })
    window.sessionStorage.setItem('motet.oauthState', 'st_1')
    window.history.replaceState({}, '', '/oauth/callback?code=abc123&state=st_1')
    render(<App />)
    await screen.findByText(/is connected/)

    const before = window.history.length

    fireEvent.click(screen.getByRole('button', { name: 'Back to Motet' }))

    expect(await screen.findByRole('heading', { name: 'Sources', level: 1 })).toBeDefined()
    expect(screen.getByRole('navigation', { name: 'Screens' })).toBeDefined()
    // Handed over with `replace`: the callback's own history entry now *is* Sources, so
    // Back cannot return to a page holding a spent code.
    expect(window.location.pathname).toBe('/sources')
    expect(window.location.search).toBe('')
    expect(window.history.length).toBe(before)
  })

  it('hands a consent this tab did not begin back to the iOS app, and exchanges nothing', async () => {
    // The app opens Google in the system sign-in sheet, which has an empty sessionStorage,
    // and waits for motet://consent. Exchanging here instead — with no session in the
    // sheet — was a 401 the app never heard about.
    const calls = mockApi({ '/v1/sources/callback': { ...GMAIL_SOURCE, connected: true } })
    window.history.replaceState({}, '', '/oauth/callback?code=abc123&state=st_app')

    render(<App />)

    expect(await screen.findByRole('heading', { name: 'Back to the Motet app' })).toBeDefined()
    expect(calls.find((call) => call.url.includes('/v1/sources/callback'))).toBeUndefined()

    // Somebody who really did begin it in another tab of this browser can still finish.
    fireEvent.click(screen.getByRole('button', { name: 'Finish here instead' }))
    expect(await screen.findByText(/is connected/)).toBeDefined()
    expect(calls.filter((call) => call.url.includes('/v1/sources/callback'))).toHaveLength(1)
  })

  it('renders without the shell while it is on screen', async () => {
    mockApi({ '/v1/sources/callback': { ...GMAIL_SOURCE, connected: true, active: true } })
    window.sessionStorage.setItem('motet.oauthState', 'st_1')
    window.history.replaceState({}, '', '/oauth/callback?code=abc123&state=st_1')
    render(<App />)

    await screen.findByText(/is connected/)
    expect(screen.queryByRole('navigation', { name: 'Screens' })).toBeNull()
    // And the shell does not rewrite the address underneath it: `forgetCallbackUrl` owns
    // this path until the person chooses to leave it.
    expect(window.location.pathname).toBe('/')
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
    expect(screen.getByRole('button', { name: 'Start listening' })).toBeDefined()
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

    fireEvent.click(screen.getByRole('button', { name: 'Start listening' }))

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

    fireEvent.click(screen.getByRole('button', { name: 'Start listening' }))

    expect(await screen.findByText(/MOTET_ALLOWED_EMAILS is unset/)).toBeDefined()
  })
})

describe("the /oauth/callback landing, for an MCP client's authorization", () => {
  it('sends an mcp. state to its own route, and to neither of the others', async () => {
    // A state spent at the wrong route is burnt, and the person starts again at the agent.
    const calls = mockApi({
      '/v1/auth/mcp/callback': {
        client_name: 'Claude Desktop',
        redirect_host: 'claude.example',
        email: 'owner@motet.test',
        redirect_url: 'https://claude.example/callback?code=c1&state=s1',
        deny_url: 'https://claude.example/callback?error=access_denied&state=s1',
      },
    })
    window.history.replaceState({}, '', '/oauth/callback?code=abc123&state=mcp.st_1')

    render(<App />)

    expect(await screen.findByRole('button', { name: 'Allow' })).toBeDefined()
    expect(screen.queryByRole('navigation', { name: 'Screens' })).toBeNull()
    const callback = calls.find((call) => call.url.includes('/v1/auth/mcp/callback'))
    expect(callback?.body).toEqual({ state: 'mcp.st_1', code: 'abc123' })
    expect(calls.find((call) => call.url.includes('/v1/auth/google/callback'))).toBeUndefined()
    expect(calls.find((call) => call.url.includes('/v1/sources/callback'))).toBeUndefined()
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
    window.sessionStorage.setItem('motet.oauthState', 'st_mailbox')
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

  it('hands a finished sign-in to the front of the app, without a history entry for the code', async () => {
    window.localStorage.clear()
    mockApi({
      '/v1/auth/google/callback': {
        token: 'sess_abc',
        email: 'owner@motet.test',
        expires_at: '2026-09-23T00:00:00Z',
      },
    })
    window.sessionStorage.setItem('motet.oauthState', 'login.st_1')
    window.history.replaceState({}, '', '/oauth/callback?code=abc123&state=login.st_1')
    const before = window.history.length
    render(<App />)
    await screen.findByText(/Signed in as owner@motet.test/)
    expect(screen.queryByRole('navigation', { name: 'Screens' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Back to Motet' }))

    // `/`, which is the Backlog, and which the shell then names in the address bar — all
    // by `replace`, so the entry the code arrived on is gone rather than one Back away.
    expect(await screen.findByRole('heading', { name: 'Backlog', level: 1 })).toBeDefined()
    await waitFor(() => expect(window.location.pathname).toBe('/backlog'))
    expect(window.location.search).toBe('')
    expect(window.history.length).toBe(before)
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

    expect(await screen.findByRole('heading', { name: 'Backlog', level: 1 })).toBeDefined()
    expect(screen.getByRole('navigation', { name: 'Screens' })).toBeDefined()
  })
})

describe('signing out', () => {
  it('says who is signed in and revokes the session', async () => {
    const calls = mockApi({ '/v1/auth/logout': {} })
    render(<App />)
    // The address is the account button in the top bar; the menu behind it holds Sign out.
    fireEvent.click(await screen.findByRole('button', { name: /owner@motet.test/ }))
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

    await screen.findByRole('heading', { name: 'Backlog', level: 1 })
    // Opened, so that the assertion is about the menu's contents and not about it being shut.
    fireEvent.click(screen.getByRole('button', { name: 'Account' }))
    expect(screen.getByText('Using the shared API token.')).toBeDefined()
    expect(screen.queryByRole('button', { name: 'Sign out' })).toBeNull()
  })
})

describe('the admin view', () => {
  const ADMIN_SESSION: SessionInfo = { ...SESSION, email: 'operator@motet.test', admin: true }

  const job = (id: number): AdminOverview['jobs'][number] => ({
    id,
    queue: 'integrate',
    state: 'failed',
    attempts: 5,
    user_id: 'motet-owner',
    subject: `si_${id}`,
    last_error: 'upstream timed out',
    run_at: '2026-09-13T00:00:00Z',
    created_at: '2026-09-13T00:00:00Z',
    updated_at: '2026-09-13T00:00:00Z',
    locked_at: null,
  })

  const OVERVIEW: AdminOverview = {
    generated_at: '2026-09-13T00:00:00Z',
    queues: [
      {
        queue: 'integrate',
        ready: 3,
        running: 1,
        done: 40,
        failed: 2,
        oldest_ready_age_s: 12,
        last_heartbeat_at: '2026-09-13T00:00:00Z',
      },
    ],
    users: [
      {
        user_id: 'motet-owner',
        email: null,
        source_items: { held: 5, pending: 3, integrated: 40, failed: 2, dismissed: 1 },
        news_items: { unread: 12, read: 20 },
        episodes: { pending: 0, scripting: 0, rendering: 0, ready: 1, failed: 0 },
        jobs: { ready: 3, running: 1, done: 40, failed: 2 },
      },
    ],
    jobs: [job(9), job(8)],
    jobs_next_before: 8,
  }

  const overviewCalls = (calls: { url: string }[]) =>
    calls.filter((call) => call.url.includes('/v1/admin/overview'))

  it('links to the admin view only for a caller the server says is an admin', async () => {
    mockApi({ '/v1/auth/session': ADMIN_SESSION })
    const { unmount } = render(<App />)
    const link = await screen.findByRole('link', { name: 'Admin' })
    expect(link.getAttribute('href')).toBe('/admin')
    unmount()

    mockApi()
    render(<App />)
    await screen.findByText('owner@motet.test', { exact: false })
    expect(screen.queryByRole('link', { name: 'Admin' })).toBeNull()
  })

  it('refuses a signed-in non-admin at /admin without asking for anybody’s data', async () => {
    window.history.replaceState({}, '', '/admin')
    const calls = mockApi({ '/v1/admin/overview': OVERVIEW })
    render(<App />)

    expect((await screen.findByRole('alert')).textContent).toContain('is not an admin')
    expect(overviewCalls(calls)).toEqual([])
    // Still a section of the shell, and still at its own address — just not one the
    // sidebar offers to this caller.
    expect(screen.getByRole('heading', { name: 'Admin', level: 1 })).toBeDefined()
    expect(screen.getByRole('navigation', { name: 'Screens' })).toBeDefined()
    expect(screen.queryByRole('link', { name: 'Admin' })).toBeNull()
    expect(window.location.pathname).toBe('/admin')
  })

  it('says it is checking, not that the caller is refused, until the session answers', async () => {
    window.history.replaceState({}, '', '/admin')
    const calls = mockApi({ '/v1/admin/overview': OVERVIEW })
    const answered = vi.mocked(fetch).getMockImplementation()!
    // The session question never comes back; everything else is answered as usual.
    vi.mocked(fetch).mockImplementation((input, init) =>
      String(input).includes('/v1/auth/session') ? new Promise(() => undefined) : answered(input, init),
    )
    render(<App />)

    expect(await screen.findByText('Checking whether this account is an admin…')).toBeDefined()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(overviewCalls(calls)).toEqual([])
  })

  it('refuses the shared API token at /admin, which belongs to no person', async () => {
    window.history.replaceState({}, '', '/admin')
    const calls = mockApi({
      '/v1/auth/session': { ...SESSION, how: 'token', email: null, expires_at: null },
      '/v1/admin/overview': OVERVIEW,
    })
    render(<App />)

    expect((await screen.findByRole('alert')).textContent).toContain('shared API token')
    expect(overviewCalls(calls)).toEqual([])
  })

  it('renders the overview for an admin and pages through older jobs', async () => {
    window.history.replaceState({}, '', '/admin')
    const calls = mockApi({ '/v1/auth/session': ADMIN_SESSION, '/v1/admin/overview': OVERVIEW })
    render(<App />)

    expect(await screen.findByText('si_9')).toBeDefined()
    expect(screen.getAllByText('upstream timed out', { selector: 'td' })).toHaveLength(2)
    expect(overviewCalls(calls)[0]?.url).toMatch(/\/v1\/admin\/overview$/)

    fireEvent.click(screen.getByRole('button', { name: 'Older jobs' }))
    await waitFor(() =>
      expect(overviewCalls(calls).at(-1)?.url).toMatch(/\/v1\/admin\/overview\?before=8$/),
    )
  })

  it('scopes the jobs to a user on the server when a user row is clicked', async () => {
    window.history.replaceState({}, '', '/admin')
    const calls = mockApi({ '/v1/auth/session': ADMIN_SESSION, '/v1/admin/overview': OVERVIEW })
    render(<App />)

    fireEvent.click(await screen.findByText('motet-owner', { selector: 'strong' }))
    await waitFor(() =>
      expect(overviewCalls(calls).at(-1)?.url).toMatch(/\?user_id=motet-owner$/),
    )
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
      // Required and nullable, like `worker_last_seen_at`: the field is always
      // present, and null is the answer when nothing named the build.
      revision: null,
      telemetry_configured: false,
      telemetry_exporting: false,
      errors_configured: false,
      authenticated: true,
      login_configured: true,
      // Off unless this deployment serves an app-site-association file naming the app.
      ios_app_link: false,
      vault_backend: 'kms',
      vault_ready: true,
      drain_trigger: true,
      voice_configured: false,
      // Off unless this deployment has an enrichment service wired (motet#102).
      enrich_enabled: false,
      mcp_oauth_configured: false,
      mcp_tools: 0,
      inference_mode: 'fake',
      settings_writable: false,
      llm_overrides_in_force: false,
    }
    expect(health.status).toBe('ok')
  })
})
