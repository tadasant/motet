// What a backlog row is called, and what is behind it.
//
// The rule is the server's (`display_title`), so what is under test here is that the
// screen renders it, shows the merge affordance when there is one, and that a row is a way
// into the provenance rather than a place a summary is repeated.

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { NewsItem, NewsItemDetail as Detail } from '../api/client'
import { Backlog } from './Backlog'

const SINGLE: NewsItem = {
  id: 'ni_1',
  title: 'A regulator asked about retention',
  display_title: 'Platformer: the retention inquiry nobody asked for',
  summary: 'The agency confirmed an inquiry.',
  source_item_ids: ['si_1'],
  sources: [{ id: 'si_1', title: 'Platformer: the retention inquiry nobody asked for' }],
  read: false,
  created_at: '2026-09-18T07:00:00Z',
}

const MERGED: NewsItem = {
  id: 'ni_2',
  title: 'Acme raises $20M, say several',
  display_title: 'Acme raises $20M, say several',
  summary: 'Two newsletters covered the round.',
  source_item_ids: ['si_2', 'si_3'],
  sources: [
    { id: 'si_2', title: 'The Download — Tuesday' },
    { id: 'si_3', title: 'Import AI' },
  ],
  read: false,
  created_at: '2026-09-19T07:00:00Z',
}

const DETAIL: Detail = {
  id: 'ni_2',
  title: 'Acme raises $20M, say several',
  display_title: 'Acme raises $20M, say several',
  summary: 'Two newsletters covered the round.',
  read: false,
  created_at: '2026-09-19T07:00:00Z',
  sources: [
    {
      id: 'si_2',
      title: 'The Download — Tuesday',
      source_kind: 'gmail',
      source_name: 'Newsletters',
      received_at: '2026-09-19T06:00:00Z',
      chars: 1_800,
      preview: 'Acme raised twenty million dollars on Tuesday.',
      position: 0,
    },
    {
      id: 'si_3',
      title: 'Import AI',
      source_kind: 'gmail',
      source_name: 'Newsletters',
      received_at: '2026-09-19T06:30:00Z',
      chars: 900,
      preview: "Acme's Series A closed this week.",
      position: 1,
    },
  ],
}

function mockFetch(routes: Record<string, unknown>): { url: string; method: string }[] {
  const calls: { url: string; method: string }[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://api.test').pathname
      const method = init?.method ?? 'GET'
      calls.push({ url, method })
      const key = `${method} ${url}`
      const found = key in routes
      return {
        ok: found,
        status: found ? 200 : 404,
        statusText: found ? 'OK' : 'Not Found',
        json: async () => (found ? routes[key] : { detail: 'not found' }),
      } as Response
    }),
  )
  return calls
}

function show(items: NewsItem[]) {
  return render(
    <Backlog
      items={items}
      ingestion={[]}
      ingestionUnavailable={false}
      processing={null}
      onChanged={() => {}}
      onOpenEpisode={() => {}}
    />,
  )
}

beforeEach(() => {
  window.localStorage.setItem('motet.apiToken', 'test-token')
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('a backlog row', () => {
  it('is titled by its one source, verbatim, and carries no source count', async () => {
    mockFetch({ 'GET /v1/source-items/held': [] })
    show([SINGLE])

    expect(
      screen.getByRole('button', {
        name: 'Platformer: the retention inquiry nobody asked for',
      }),
    ).toBeDefined()
    // The line *is* that source's title, so "1 source" would be saying it twice.
    expect(screen.queryByText(/1 source/)).toBeNull()
    // The summary moved to the detail: a row is a glance.
    expect(screen.queryByText('The agency confirmed an inquiry.')).toBeNull()
  })

  it('wears dedup’s title and says how many write-ups are behind it when merged', async () => {
    mockFetch({ 'GET /v1/source-items/held': [] })
    show([MERGED])

    expect(screen.getByRole('button', { name: 'Acme raises $20M, say several' })).toBeDefined()
    expect(screen.getByText('2 sources')).toBeDefined()
  })

  it('falls back to the stored title on an API that does not send a display title', async () => {
    mockFetch({ 'GET /v1/source-items/held': [] })
    // `display_title` is optional on the wire for exactly this: a client newer than the
    // deployment it is pointed at still renders a row rather than a blank line.
    show([{ ...SINGLE, display_title: '' }])

    expect(screen.getByRole('button', { name: 'A regulator asked about retention' })).toBeDefined()
  })

  it('opens the provenance, which names every source and quotes each one', async () => {
    mockFetch({
      'GET /v1/source-items/held': [],
      'GET /v1/news-items/ni_2': DETAIL,
    })
    show([MERGED])

    fireEvent.click(screen.getByRole('button', { name: 'Acme raises $20M, say several' }))

    const story = await screen.findByRole('region', { name: 'Story' })
    // Title and summary at the top, then the write-ups, in the order dedup merged them.
    expect(within(story).getByRole('heading', { name: 'Acme raises $20M, say several' })).toBeDefined()
    expect(within(story).getByText('Two newsletters covered the round.')).toBeDefined()
    expect(within(story).getByText('The Download — Tuesday')).toBeDefined()
    expect(within(story).getByText('Import AI')).toBeDefined()
    expect(
      within(story).getByText('Acme raised twenty million dollars on Tuesday.'),
    ).toBeDefined()
    expect(within(story).getByText(/started this story/)).toBeDefined()
    expect(within(story).getByText(/merged in/)).toBeDefined()

    // And back is a link, not a URL: the shell has one path per section.
    fireEvent.click(within(story).getByRole('button', { name: '← Backlog' }))
    await waitFor(() => expect(screen.getByRole('region', { name: 'Backlog' })).toBeDefined())
  })

  it('asks the API for a title-free episode, because the server names it', async () => {
    const calls = mockFetch({
      'GET /v1/source-items/held': [],
      'POST /v1/episodes': { id: 'ep_1' },
    })
    show([SINGLE])

    fireEvent.click(screen.getByRole('button', { name: 'Make an episode' }))

    await waitFor(() =>
      expect(calls.some((call) => call.method === 'POST' && call.url === '/v1/episodes')).toBe(true),
    )
  })
})
