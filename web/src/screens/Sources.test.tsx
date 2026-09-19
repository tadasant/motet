// The Sources screen as an integrations catalog.
//
// The 'connecting a mailbox' tests that used to live in App.test.tsx are here, rewritten
// for the catalog: what each card's pill says for a connected, unconnected and
// coming-soon integration, that Connect still starts consent through the API exactly as
// before, and that an unfinished consent reads as "you cancelled" rather than as a fault.

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HeldSourceItem, IngestionItem, ProcessingStatus, Source } from '../api/client'
import { Sources } from './Sources'
import { CATALOG } from './sources/catalog'
import {
  cardStatus,
  countsFor,
  describeLastSync,
  relativeTime,
  rowStatus,
} from './sources/status'

const NOW = new Date('2026-09-13T00:00:00Z').getTime()

/** What every source row carries beyond its identity: nothing synced, nothing pulled in. */
const ROW_DEFAULTS = {
  disconnected_at: null,
  last_sync: null,
  query: 'category:updates OR category:promotions',
  first_sync_days: null,
  items_pulled_in: 0,
  items_integrated: 0,
} satisfies Partial<Source>

/** The built-in source, exactly as `GET /v1/sources` reports it: active, never connected. */
const PASTE_SOURCE: Source = {
  ...ROW_DEFAULTS,
  id: 'src_paste',
  kind: 'paste',
  name: 'Pasted text',
  active: true,
  connected: false,
  scopes: [],
  last_polled_at: null,
  last_error: null,
  created_at: '2026-08-24T00:00:00Z',
  query: null,
}

/** The row `POST /v1/sources/connect` creates before the user leaves for Google. */
const PENDING_GMAIL: Source = {
  ...ROW_DEFAULTS,
  id: 'src_1',
  kind: 'gmail',
  name: 'Gmail',
  active: false,
  connected: false,
  scopes: [],
  last_polled_at: null,
  last_error: null,
  created_at: '2026-09-12T23:00:00Z',
}

const CONNECTED_GMAIL: Source = {
  ...ROW_DEFAULTS,
  id: 'src_2',
  kind: 'gmail',
  name: 'Gmail (owner@motet.test)',
  active: true,
  connected: true,
  scopes: ['https://www.googleapis.com/auth/gmail.readonly'],
  last_polled_at: '2026-09-12T23:30:00Z',
  last_error: null,
  created_at: '2026-09-10T00:00:00Z',
  last_sync: { at: '2026-09-12T23:30:00Z', seen: 50, queued: 10, error: null, caught_up: true },
  query: 'from:newsletter@example.test',
  first_sync_days: 7,
  items_pulled_in: 41,
  items_integrated: 30,
}

const HELD: HeldSourceItem[] = [
  {
    id: 'si_h1',
    title: 'A newsletter',
    source_id: 'src_2',
    source_kind: 'gmail',
    source_name: CONNECTED_GMAIL.name,
    received_at: '2026-09-12T23:31:00Z',
    chars: 2000,
    preview: 'A newsletter about things.',
  },
  {
    id: 'si_h2',
    title: 'Another',
    source_id: 'src_2',
    source_kind: 'gmail',
    source_name: CONNECTED_GMAIL.name,
    received_at: '2026-09-12T23:32:00Z',
    chars: 900,
    preview: 'Another one.',
  },
]

const ingestionItem = (overrides: Partial<IngestionItem>): IngestionItem => ({
  id: 'si_x',
  title: 'x',
  state: 'pending',
  attempts: 0,
  max_attempts: 5,
  next_attempt_at: null,
  last_error: null,
  created_at: '2026-09-12T23:40:00Z',
  source_kind: 'gmail',
  source_id: 'src_2',
  ...overrides,
})

const PROCESSING: ProcessingStatus = {
  now: '2026-09-13T00:00:00Z',
  worker_last_seen_at: '2026-09-12T23:59:50Z',
  queues: [],
  readiness: [],
}

/**
 * Route a fake fetch by URL. A key may carry a method (`'POST /v1/sources/src_2/poll'`)
 * and those match first and exactly; the rest match by prefix, longest first.
 */
function mockApi(overrides: Record<string, unknown> = {}) {
  const calls: { url: string; method: string; body: unknown }[] = []
  const routes: Record<string, unknown> = {
    '/v1/sources': [PASTE_SOURCE, PENDING_GMAIL],
    '/v1/source-items/held': [],
    '/v1/ingestion': [],
    '/v1/processing': PROCESSING,
    ...overrides,
  }
  for (const [route, value] of Object.entries(routes)) {
    if (value === undefined) delete routes[route]
  }
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
    const names = Object.keys(routes)
    const key =
      names.filter((route) => route.includes(' ')).find((route) => `${method} ${url}` === route) ??
      names
        .filter((route) => !route.includes(' '))
        .sort((a, b) => b.length - a.length)
        .find((route) => url.startsWith(route))
    const value = key === undefined ? undefined : routes[key]
    if (value instanceof Error) {
      throw value
    }
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

const card = (name: string) => screen.getByRole('article', { name: new RegExp(`^${name}:`) })

beforeEach(() => {
  window.localStorage.clear()
  window.sessionStorage.clear()
  window.localStorage.setItem('motet.apiToken', 'test-token')
})

afterEach(() => {
  vi.unstubAllGlobals()
  window.history.replaceState({}, '', '/')
})

describe('the catalog', () => {
  it('shows every integration with the right pill, and offers a button only where the API can keep the promise', async () => {
    mockApi({ '/v1/sources': [PASTE_SOURCE, CONNECTED_GMAIL] })
    render(<Sources navigate={vi.fn()} now={NOW} />)
    await screen.findByRole('article', { name: /^Gmail:/ })

    expect(card('Gmail').getAttribute('aria-label')).toBe('Gmail: Connected')
    expect(card('Paste').getAttribute('aria-label')).toBe('Paste: Always on')
    expect(card('X bookmarks').getAttribute('aria-label')).toBe('X bookmarks: Coming soon')
    expect(card('RSS').getAttribute('aria-label')).toBe('RSS: Coming soon')

    // A connected Gmail is managed, not connected again.
    expect(within(card('Gmail')).getByRole('button', { name: /Manage|Close/ })).toBeDefined()
    // The API answers 400 for any other provider: no "Connect X", only a disabled
    // "Coming soon" that says why on hover.
    expect(screen.queryByRole('button', { name: /Connect X/ })).toBeNull()
    const x = within(card('X bookmarks')).getByRole('button', { name: 'Coming soon' })
    expect(x.hasAttribute('disabled')).toBe(true)
    expect(x.getAttribute('title')).toMatch(/X API tier/)
  })

  it('reads an unconnected account as not connected and points a fresh account at Gmail first', async () => {
    mockApi({ '/v1/sources': [PASTE_SOURCE] })
    render(<Sources navigate={vi.fn()} now={NOW} />)
    await screen.findByRole('article', { name: /^Gmail:/ })

    expect(card('Gmail').getAttribute('aria-label')).toBe('Gmail: Not connected')
    expect(card('Gmail').className).toContain('highlighted')
    expect(within(card('Gmail')).getByText('Start here')).toBeDefined()
    // The empty state opens the connect form on its own: nothing else to do here.
    expect(screen.getByRole('form', { name: 'Connect Gmail' })).toBeDefined()
  })

  it('reads the paste source off `active`, because consent and polling do not apply to it', async () => {
    // motet#39. `connected: false` is not a state the paste row is passing through.
    mockApi({ '/v1/sources': [PASTE_SOURCE] })
    render(<Sources navigate={vi.fn()} now={NOW} />)
    await screen.findByRole('article', { name: /^Paste:/ })

    expect(card('Paste').getAttribute('aria-label')).toBe('Paste: Always on')
    fireEvent.click(within(card('Paste')).getByRole('button', { name: 'Paste in' }))
    expect(window.location.pathname).toBe('/paste')
    // Nothing polls pasted text, so "Never polled" would read as a fetch that never fired.
    expect(screen.queryByText(/Never polled/)).toBeNull()
  })

  it('opens the detail of the one connected mailbox on its own, and shows what it has pulled in', async () => {
    mockApi({
      '/v1/sources': [PASTE_SOURCE, CONNECTED_GMAIL],
      '/v1/source-items/held': HELD,
      '/v1/ingestion': [
        ingestionItem({ id: 'si_p1' }),
        ingestionItem({ id: 'si_f1', state: 'failed', last_error: 'boom' }),
        ingestionItem({ id: 'si_paste', source_kind: 'paste', source_id: 'src_paste' }),
        // Another mailbox's work is its own, not this one's.
        ingestionItem({ id: 'si_other', source_id: 'src_9' }),
      ],
    })
    render(<Sources navigate={vi.fn()} now={NOW} />)

    const detail = await screen.findByRole('region', { name: 'Gmail details' })
    expect(within(detail).getByText('Gmail (owner@motet.test)')).toBeDefined()
    expect(within(detail).getByText('30 minutes ago')).toBeDefined()
    expect(within(detail).getByText('read-only mail')).toBeDefined()

    const stats = within(detail).getByLabelText('What this source has pulled in')
    expect(stats.textContent).toBe('2Waiting for you1Processing1Failed0Landed recently')
    expect(within(detail).getByText('2 items waiting')).toBeDefined()

    // What the last sync found, the filter it ran, the window, and the all-time totals.
    expect(within(detail).getByText('Looked at 50 messages; 10 were new.')).toBeDefined()
    expect(within(detail).getByText('from:newsletter@example.test')).toBeDefined()
    expect(within(detail).getByText(/first sync reached back 7 days/)).toBeDefined()
    expect(within(detail).getByText('41 pulled in, 30 ingested')).toBeDefined()

    fireEvent.click(within(detail).getByRole('button', { name: 'Review them in Backlog' }))
    expect(window.location.pathname).toBe('/backlog')
  })

  it('queues a poll on Sync now and reports it synced once the last sync moves', async () => {
    let polled = false
    const later: Source = {
      ...CONNECTED_GMAIL,
      last_polled_at: '2026-09-13T00:00:30Z',
      last_sync: { at: '2026-09-13T00:00:30Z', seen: 50, queued: 1, error: null, caught_up: true },
    }
    const calls = mockApi({
      '/v1/sources': [PASTE_SOURCE, CONNECTED_GMAIL],
      'POST /v1/sources/src_2/poll': CONNECTED_GMAIL,
    })
    // After the poll is queued, the next sources fetch reports the poll having run.
    const fetchMock = vi.mocked(fetch)
    const original = fetchMock.getMockImplementation()!
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/poll')) polled = true
      if (polled && url.endsWith('/v1/sources') && (init?.method ?? 'GET') === 'GET') {
        return { ok: true, status: 200, json: async () => [PASTE_SOURCE, later] } as Response
      }
      return original(input, init)
    })

    render(<Sources navigate={vi.fn()} now={NOW} />)
    const detail = await screen.findByRole('region', { name: 'Gmail details' })
    fireEvent.click(within(detail).getByRole('button', { name: 'Sync now' }))

    expect(await within(detail).findByRole('button', { name: 'Syncing…' })).toBeDefined()
    expect(within(detail).getByText(/Queued — a worker is running/)).toBeDefined()
    await waitFor(
      () => expect(calls.find((c) => c.method === 'POST' && c.url.endsWith('/v1/sources/src_2/poll'))).toBeDefined(),
    )
    // The watcher re-fetches every two seconds; the fixture answers with the moved stamp.
    expect(await within(detail).findByText(/^Synced /, {}, { timeout: 4_000 })).toBeDefined()
    expect(within(detail).getByRole('button', { name: 'Sync now' })).toBeDefined()
    expect(within(detail).getByText('Looked at 50 messages; 1 was new.')).toBeDefined()
  })

  it('keeps watching when a re-fetch comes back unchanged, and reports the sync when it lands', async () => {
    // The first re-fetch after queueing answers with the old sync; only a later one has
    // moved. A watch that re-armed only on a change stopped at the first answer.
    const later: Source = {
      ...CONNECTED_GMAIL,
      last_polled_at: '2026-09-13T00:00:30Z',
      last_sync: { at: '2026-09-13T00:00:30Z', seen: 0, queued: 0, error: null, caught_up: true },
    }
    let polled = false
    let refetches = 0
    mockApi({
      '/v1/sources': [PASTE_SOURCE, CONNECTED_GMAIL],
      'POST /v1/sources/src_2/poll': CONNECTED_GMAIL,
    })
    const fetchMock = vi.mocked(fetch)
    const original = fetchMock.getMockImplementation()!
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/poll')) polled = true
      if (polled && url.endsWith('/v1/sources') && (init?.method ?? 'GET') === 'GET') {
        refetches += 1
        const rows = refetches >= 2 ? [PASTE_SOURCE, later] : [PASTE_SOURCE, CONNECTED_GMAIL]
        return { ok: true, status: 200, json: async () => rows } as Response
      }
      return original(input, init)
    })

    render(<Sources navigate={vi.fn()} now={NOW} />)
    const detail = await screen.findByRole('region', { name: 'Gmail details' })
    fireEvent.click(within(detail).getByRole('button', { name: 'Sync now' }))

    expect(await within(detail).findByText(/^Synced /, {}, { timeout: 7_000 })).toBeDefined()
    expect(refetches).toBeGreaterThanOrEqual(2)
  }, 10_000)

  it('does not call it synced when only an extraction moved last_polled_at', async () => {
    // `_record_skip` moves `last_polled_at` too. The poll has run when `last_sync.at` moves.
    const skipped: Source = { ...CONNECTED_GMAIL, last_polled_at: '2026-09-13T00:00:10Z' }
    let polled = false
    mockApi({
      '/v1/sources': [PASTE_SOURCE, CONNECTED_GMAIL],
      'POST /v1/sources/src_2/poll': CONNECTED_GMAIL,
    })
    const fetchMock = vi.mocked(fetch)
    const original = fetchMock.getMockImplementation()!
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/poll')) polled = true
      if (polled && url.endsWith('/v1/sources') && (init?.method ?? 'GET') === 'GET') {
        return { ok: true, status: 200, json: async () => [PASTE_SOURCE, skipped] } as Response
      }
      return original(input, init)
    })

    render(<Sources navigate={vi.fn()} now={NOW} />)
    const detail = await screen.findByRole('region', { name: 'Gmail details' })
    fireEvent.click(within(detail).getByRole('button', { name: 'Sync now' }))
    await within(detail).findByRole('button', { name: 'Syncing…' })
    // Two watcher ticks with the skip's timestamp and no new sync: still queued.
    await new Promise((resolve) => setTimeout(resolve, 4_500))
    expect(within(detail).getByRole('button', { name: 'Syncing…' })).toBeDefined()
    expect(within(detail).queryByText(/^Synced /)).toBeNull()
  }, 10_000)

  it('says the last sync gave up when it records an error', async () => {
    const expired: Source = {
      ...CONNECTED_GMAIL,
      last_sync: {
        at: '2026-09-12T23:30:00Z',
        seen: 0,
        queued: 0,
        error: 'RuntimeError: gmail returned 503',
        caught_up: false,
      },
    }
    mockApi({ '/v1/sources': [PASTE_SOURCE, expired] })
    render(<Sources navigate={vi.fn()} now={NOW} />)
    const detail = await screen.findByRole('region', { name: 'Gmail details' })
    expect(within(detail).getByText('The last sync gave up: RuntimeError: gmail returned 503')).toBeDefined()
  })

  it('marks the default filter as the default', async () => {
    const byDefault: Source = { ...CONNECTED_GMAIL, query: ROW_DEFAULTS.query }
    mockApi({ '/v1/sources': [PASTE_SOURCE, byDefault] })
    render(<Sources navigate={vi.fn()} now={NOW} />)
    const detail = await screen.findByRole('region', { name: 'Gmail details' })
    expect(within(detail).getByText(ROW_DEFAULTS.query)).toBeDefined()
    expect(within(detail).getByText('(the default)')).toBeDefined()
  })

  it('disconnects only after a confirmation, through the credentials route', async () => {
    const calls = mockApi({
      '/v1/sources': [PASTE_SOURCE, CONNECTED_GMAIL],
      'DELETE /v1/sources/src_2/credentials': null,
    })
    render(<Sources navigate={vi.fn()} now={NOW} />)
    const detail = await screen.findByRole('region', { name: 'Gmail details' })

    fireEvent.click(within(detail).getByRole('button', { name: 'Disconnect' }))
    expect(calls.find((c) => c.method === 'DELETE')).toBeUndefined()
    expect(within(detail).getByText(/what it pulled in stays/)).toBeDefined()

    fireEvent.click(within(detail).getByRole('button', { name: 'Yes, disconnect' }))
    await waitFor(() =>
      expect(calls.find((c) => c.method === 'DELETE')?.url).toMatch(/\/v1\/sources\/src_2\/credentials$/),
    )
  })
})

describe('when something goes wrong', () => {
  it('reports a sync that gave up as a failure, not as synced', async () => {
    // A poll that gave up moves `last_sync.at` too, and records why on `last_sync.error`.
    const gaveUp: Source = {
      ...CONNECTED_GMAIL,
      last_sync: {
        at: '2026-09-13T00:00:30Z',
        seen: 0,
        queued: 0,
        caught_up: false,
        error: 'SourceAuthError: invalid_grant',
      },
    }
    let polled = false
    mockApi({
      '/v1/sources': [PASTE_SOURCE, CONNECTED_GMAIL],
      'POST /v1/sources/src_2/poll': CONNECTED_GMAIL,
    })
    const fetchMock = vi.mocked(fetch)
    const original = fetchMock.getMockImplementation()!
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/poll')) polled = true
      if (polled && url.endsWith('/v1/sources') && (init?.method ?? 'GET') === 'GET') {
        return { ok: true, status: 200, json: async () => [PASTE_SOURCE, gaveUp] } as Response
      }
      return original(input, init)
    })

    render(<Sources navigate={vi.fn()} now={NOW} />)
    const detail = await screen.findByRole('region', { name: 'Gmail details' })
    fireEvent.click(within(detail).getByRole('button', { name: 'Sync now' }))

    const alert = await within(detail).findByText(/The sync gave up: SourceAuthError/, {}, { timeout: 4_000 })
    expect(alert.getAttribute('role')).toBe('alert')
    expect(within(detail).queryByText(/^Synced /)).toBeNull()
  }, 10_000)

  it('keeps the rows on screen when a re-fetch fails', async () => {
    // One transient error during a watch must not read as a fresh account.
    let calls = 0
    mockApi({ '/v1/sources': [PASTE_SOURCE, CONNECTED_GMAIL], 'POST /v1/sources/src_2/poll': CONNECTED_GMAIL })
    const fetchMock = vi.mocked(fetch)
    const original = fetchMock.getMockImplementation()!
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/v1/sources') && (init?.method ?? 'GET') === 'GET') {
        calls += 1
        if (calls >= 2) {
          return { ok: false, status: 502, statusText: 'Bad Gateway', json: async () => ({ detail: 'upstream' }) } as Response
        }
      }
      return original(input, init)
    })

    render(<Sources navigate={vi.fn()} now={NOW} />)
    const detail = await screen.findByRole('region', { name: 'Gmail details' })
    fireEvent.click(within(detail).getByRole('button', { name: 'Sync now' }))

    await waitFor(() => expect(calls).toBeGreaterThanOrEqual(2), { timeout: 4_000 })
    expect(await screen.findByRole('alert')).toBeDefined()
    expect(card('Gmail').getAttribute('aria-label')).toBe('Gmail: Connected')
    expect(card('Gmail').className).not.toContain('highlighted')
    expect(within(screen.getByRole('region', { name: 'Gmail details' })).getByText('Gmail (owner@motet.test)')).toBeDefined()
  }, 10_000)

  it('shows the mailbox as disconnected once the credential is forgotten', async () => {
    let disconnected = false
    const forgotten: Source = {
      ...CONNECTED_GMAIL,
      connected: false,
      active: false,
      scopes: [],
      disconnected_at: '2026-09-12T23:59:00Z',
    }
    mockApi({ 'DELETE /v1/sources/src_2/credentials': null })
    const fetchMock = vi.mocked(fetch)
    const original = fetchMock.getMockImplementation()!
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input)
      if ((init?.method ?? 'GET') === 'DELETE') disconnected = true
      if (url.endsWith('/v1/sources') && (init?.method ?? 'GET') === 'GET') {
        const rows = disconnected ? [PASTE_SOURCE, forgotten] : [PASTE_SOURCE, CONNECTED_GMAIL]
        return { ok: true, status: 200, json: async () => rows } as Response
      }
      return original(input, init)
    })

    render(<Sources navigate={vi.fn()} now={NOW} />)
    const detail = await screen.findByRole('region', { name: 'Gmail details' })
    fireEvent.click(within(detail).getByRole('button', { name: 'Disconnect' }))
    fireEvent.click(within(detail).getByRole('button', { name: 'Yes, disconnect' }))

    await waitFor(() => expect(card('Gmail').getAttribute('aria-label')).toBe('Gmail: Disconnected'))
    const panel = screen.getByRole('region', { name: 'Gmail details' })
    expect(within(panel).getByText(/Disconnected 1 minute ago/)).toBeDefined()
    // Nothing left to disconnect, and a disconnected mailbox is not an attempt to remove.
    expect(within(panel).queryByRole('button', { name: 'Disconnect' })).toBeNull()
    expect(within(panel).queryByRole('button', { name: 'Remove this attempt' })).toBeNull()
  })
})

describe('an abandoned consent', () => {
  it('can be removed, through the route that refuses anything that was ever connected', async () => {
    let removed = false
    const calls = mockApi({ 'DELETE /v1/sources/src_1': null })
    const fetchMock = vi.mocked(fetch)
    const original = fetchMock.getMockImplementation()!
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input)
      if ((init?.method ?? 'GET') === 'DELETE') removed = true
      if (removed && url.endsWith('/v1/sources')) {
        return { ok: true, status: 200, json: async () => [PASTE_SOURCE] } as Response
      }
      return original(input, init)
    })
    render(<Sources navigate={vi.fn()} now={NOW} />)
    const detail = await screen.findByRole('region', { name: 'Gmail details' })

    fireEvent.click(within(detail).getByRole('button', { name: 'Remove this attempt' }))

    await waitFor(() =>
      expect(calls.find((c) => c.method === 'DELETE')?.url).toMatch(/\/v1\/sources\/src_1$/),
    )
    // The credentials route is the disconnect; removing an attempt must not reach it.
    expect(calls.some((c) => c.url.endsWith('/credentials'))).toBe(false)
    await waitFor(() => expect(card('Gmail').getAttribute('aria-label')).toBe('Gmail: Not connected'))
  })

  it('shows the API refusal rather than pretending it went', async () => {
    mockApi({
      'DELETE /v1/sources/src_1': {
        status: 409,
        detail: 'This source has pulled items in, and removing it would delete them.',
      },
    })
    render(<Sources navigate={vi.fn()} now={NOW} />)
    const detail = await screen.findByRole('region', { name: 'Gmail details' })

    fireEvent.click(within(detail).getByRole('button', { name: 'Remove this attempt' }))

    const alert = await within(detail).findByRole('alert')
    expect(alert.textContent).toContain('would delete them')
  })

  it('is offered only for a row that never connected', async () => {
    const disconnected: Source = {
      ...CONNECTED_GMAIL,
      connected: false,
      active: false,
      scopes: [],
      disconnected_at: '2026-09-12T22:00:00Z',
    }
    mockApi({ '/v1/sources': [PASTE_SOURCE, CONNECTED_GMAIL, disconnected] })
    render(<Sources navigate={vi.fn()} now={NOW} />)
    // One connected mailbox, so the panel opens on its own.
    const panel = await screen.findByRole('region', { name: 'Gmail details' })
    expect(within(panel).queryByRole('button', { name: 'Remove this attempt' })).toBeNull()
    expect(within(panel).getByText(/Disconnected 2 hours ago/)).toBeDefined()
  })
})

describe('connecting a mailbox', () => {
  it('opens the connect form right under the Gmail card and brings it into view', async () => {
    // Tadas, 2026-09-19: "Web app just toggles with 'close' and 'connect'." The panel was
    // rendered after the whole grid, so on a phone it opened below three more cards and
    // nothing on screen changed but the button's label. An earlier attempt that never
    // finished is the realistic state to be in by then.
    mockApi({ '/v1/sources': [PASTE_SOURCE, PENDING_GMAIL] })
    const scrolled: Element[] = []
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = function (this: Element) {
      scrolled.push(this)
    }
    try {
      render(<Sources navigate={vi.fn()} now={NOW} />)
      await screen.findByRole('form', { name: 'Connect Gmail' })
      // The automatic open on first load does not move the page.
      expect(scrolled).toEqual([])

      const gmail = card('Gmail')
      fireEvent.click(within(gmail).getByRole('button', { name: 'Close' }))
      expect(screen.queryByRole('region', { name: 'Gmail details' })).toBeNull()
      fireEvent.click(within(gmail).getByRole('button', { name: 'Connect' }))

      const panel = screen.getByRole('region', { name: 'Gmail details' })
      expect(gmail.nextElementSibling).toBe(panel)
      expect(scrolled).toEqual([panel])
      // With nothing connected the form is the thing to do, so it comes before the
      // abandoned attempt's detail rather than a screen and a half below it.
      const form = within(panel).getByRole('form', { name: 'Connect Gmail' })
      const attempt = within(panel).getByRole('list')
      expect(form.compareDocumentPosition(attempt) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    } finally {
      Element.prototype.scrollIntoView = original
    }
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
    render(<Sources navigate={navigate} now={NOW} />)
    await screen.findByRole('form', { name: 'Connect Gmail' })

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
    expect(navigate).toHaveBeenCalledWith('https://accounts.google.test/o/oauth2/v2/auth?client_id=x')
    // Remembered before the redirect: after it, nothing in this tab gets to run.
    expect(window.sessionStorage.getItem('motet.oauthState')).toBe('st_1')
  })

  it('sends a blank query as null, which is what asks for the provider default', async () => {
    const calls = mockApi({
      '/v1/sources/connect': { source_id: 'src_2', authorization_url: 'https://accounts.google.test/', state: 'st_2' },
    })
    render(<Sources navigate={vi.fn()} now={NOW} />)
    await screen.findByRole('form', { name: 'Connect Gmail' })

    fireEvent.click(screen.getByRole('button', { name: 'Connect Gmail' }))

    await waitFor(() => {
      const connect = calls.find((call) => call.url.includes('/v1/sources/connect'))
      expect(connect?.body).toMatchObject({ query: null })
    })
  })

  it('explains what the flow will and will not do before the button', async () => {
    mockApi({ '/v1/sources': [PASTE_SOURCE] })
    render(<Sources navigate={vi.fn()} now={NOW} />)
    await screen.findByRole('form', { name: 'Connect Gmail' })

    expect(screen.getByText(/read-only/)).toBeDefined()
    expect(screen.getByText('Nothing is processed until you choose to ingest it')).toBeDefined()
  })

  it('says a 503 is the deployment and shows the API own message', async () => {
    // The dormant case: real mode with no Google OAuth client provisioned. The API names
    // the variable that is missing, which beats anything this screen could invent.
    mockApi({ '/v1/sources/connect': { status: 503, detail: 'GOOGLE_OAUTH_CLIENT_ID is not set.' } })
    render(<Sources navigate={vi.fn()} now={NOW} />)
    await screen.findByRole('form', { name: 'Connect Gmail' })

    fireEvent.click(screen.getByRole('button', { name: 'Connect Gmail' }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('cannot connect Gmail right now')
    expect(alert.textContent).toContain('GOOGLE_OAUTH_CLIENT_ID is not set')
  })

  it('says what a failed fetch means instead of showing the browser string', async () => {
    mockApi({ '/v1/sources/connect': new TypeError('Failed to fetch') })
    render(<Sources navigate={vi.fn()} now={NOW} />)
    await screen.findByRole('form', { name: 'Connect Gmail' })

    fireEvent.click(screen.getByRole('button', { name: 'Connect Gmail' }))

    const shown = await screen.findByText(/never completed/)
    expect(shown.textContent).toContain('/v1/sources/connect')
  })

  it('reads an unfinished consent as "you cancelled", not as a broken source', async () => {
    // The row `connect` creates before the redirect is what an access_denied leaves
    // behind: no credential ever arrived. It is an abandoned attempt, and the API reports
    // nothing else about it, so the screen has to say so where the row is shown.
    mockApi({ '/v1/sources': [PASTE_SOURCE, PENDING_GMAIL] })
    render(<Sources navigate={vi.fn()} now={NOW} />)
    await screen.findByRole('article', { name: /^Gmail:/ })

    expect(card('Gmail').getAttribute('aria-label')).toBe('Gmail: Awaiting consent')
    const detail = screen.getByRole('region', { name: 'Gmail details' })
    const notice = within(detail).getByRole('status')
    expect(notice.textContent).toMatch(/you cancelled on Google’s page/i)
    expect(notice.textContent).toMatch(/Nothing was connected/)
    expect(within(detail).queryByRole('alert')).toBeNull()
    // And the form is right there to try again.
    expect(within(detail).getByRole('form', { name: 'Connect Gmail' })).toBeDefined()
    // A pending row is a mailbox, so "never polled" is news about it.
    expect(within(detail).getByText('Never polled')).toBeDefined()
  })
})

describe('the pure half', () => {
  it('reads each row status off the fields that apply to its kind', () => {
    expect(rowStatus(PASTE_SOURCE)).toBe('ready')
    expect(rowStatus({ ...PASTE_SOURCE, active: false })).toBe('paused')
    expect(rowStatus(PENDING_GMAIL)).toBe('awaiting_consent')
    expect(rowStatus(CONNECTED_GMAIL)).toBe('connected')
    expect(rowStatus({ ...CONNECTED_GMAIL, active: false })).toBe('paused')
    expect(rowStatus({ ...CONNECTED_GMAIL, last_error: 'token revoked' })).toBe('error')
    // Disconnected and abandoned are both `connected: false`; `disconnected_at` is the tell,
    // and for a row disconnected before the API recorded it, having polled.
    const forgotten = { ...CONNECTED_GMAIL, connected: false, active: false, scopes: [] }
    expect(rowStatus({ ...forgotten, disconnected_at: '2026-09-12T22:00:00Z' })).toBe('disconnected')
    expect(rowStatus({ ...forgotten, disconnected_at: null })).toBe('disconnected')
    expect(
      rowStatus({ ...forgotten, disconnected_at: '2026-09-12T22:00:00Z', last_polled_at: null, last_sync: null }),
    ).toBe('disconnected')
  })

  it('folds rows into one card pill, attention first', () => {
    const gmail = CATALOG.find((entry) => entry.id === 'gmail')!
    expect(cardStatus(gmail, [])).toBe('not_connected')
    expect(cardStatus(gmail, [PENDING_GMAIL])).toBe('awaiting_consent')
    // One working mailbox and one cancelled consent is a connected Gmail.
    expect(cardStatus(gmail, [PENDING_GMAIL, CONNECTED_GMAIL])).toBe('connected')
    expect(cardStatus(gmail, [CONNECTED_GMAIL, { ...CONNECTED_GMAIL, id: 'src_3', last_error: 'x' }])).toBe('error')
    expect(cardStatus(CATALOG.find((entry) => entry.id === 'x')!, [])).toBe('coming_soon')
  })

  it('counts each mailbox by its own id, so two are two sets of counts', () => {
    const second = { ...CONNECTED_GMAIL, id: 'src_3' }
    const ingestion = [
      ingestionItem({ id: 'si_p1' }),
      ingestionItem({ id: 'si_i', state: 'integrated' }),
      ingestionItem({ id: 'si_f3', state: 'failed', source_id: 'src_3' }),
    ]
    expect(countsFor(CONNECTED_GMAIL, HELD, ingestion)).toEqual({
      held: 2,
      processing: 1,
      failed: 0,
      integrated: 1,
    })
    expect(countsFor(second, HELD, ingestion)).toEqual({
      held: 0,
      processing: 0,
      failed: 1,
      integrated: 0,
    })
  })

  it('describes the last sync in a sentence, or says there has been none', () => {
    const at = '2026-09-12T23:30:00Z'
    expect(describeLastSync(PENDING_GMAIL)).toBeNull()
    expect(describeLastSync({ ...CONNECTED_GMAIL, last_sync: { at, seen: 0, queued: 0, error: null, caught_up: true } })).toBe(
      'No new messages.',
    )
    expect(describeLastSync({ ...CONNECTED_GMAIL, last_sync: { at, seen: 1, queued: 0, error: null, caught_up: true } })).toBe(
      'Looked at 1 message; none were new.',
    )
    expect(describeLastSync({ ...CONNECTED_GMAIL, last_sync: { at, seen: 0, queued: 0, error: 'x', caught_up: true } })).toBe(
      'The last sync gave up: x',
    )
    expect(
      describeLastSync({ ...CONNECTED_GMAIL, last_sync: { at, seen: 50, queued: 50, error: null, caught_up: false } }),
    ).toBe('Looked at 50 messages; 50 were new. Still catching up: each sync queues the next.')
  })

  it('renders relative times against the clock it is given', () => {
    expect(relativeTime('2026-09-12T23:59:50Z', NOW)).toBe('just now')
    expect(relativeTime('2026-09-12T23:30:00Z', NOW)).toBe('30 minutes ago')
    expect(relativeTime('2026-09-12T20:00:00Z', NOW)).toBe('4 hours ago')
    expect(relativeTime('2026-09-10T00:00:00Z', NOW)).toBe('3 days ago')
  })
})
