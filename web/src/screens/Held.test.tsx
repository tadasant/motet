import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HeldSourceItem, SourceItemDetail as Detail } from '../api/client'
import { Held } from './Held'
import { SourceItemDetail } from './SourceItemDetail'

const HELD: HeldSourceItem[] = [
  {
    id: 'si_a',
    title: 'Acme raises $20M Series A',
    source_id: 'src_gmail',
    source_kind: 'gmail',
    source_name: 'Newsletters',
    received_at: '2026-08-18T07:02:11Z',
    chars: 1_800,
    preview: 'Acme raised $20M on Tuesday.',
  },
  {
    id: 'si_b',
    title: 'Weekly wire',
    source_id: 'src_gmail',
    source_kind: 'gmail',
    source_name: 'Newsletters',
    received_at: '2026-08-19T10:30:00Z',
    chars: 4_200,
    preview: 'This week in widgets.',
  },
]

const PULLED: Detail['pulled'] = {
  source_id: 'src_gmail',
  source_kind: 'gmail',
  source_name: 'Newsletters',
  external_id: '18f2a3b4c5',
  received_at: '2026-08-18T07:02:11Z',
  stored_at: '2026-09-13T17:04:00Z',
  chars: 28,
  text: 'Acme raised $20M on Tuesday.',
  raw_stored: false,
}

type Call = { url: string; method: string; body: unknown }

/** A fetch that answers by method and path, and remembers what it was asked. */
function mockFetch(routes: Record<string, unknown>): Call[] {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://api.test').pathname
      const method = init?.method ?? 'GET'
      calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
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

beforeEach(() => {
  window.localStorage.setItem('motet.apiToken', 'test-token')
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('Held', () => {
  it('renders nothing when nothing is held', async () => {
    const calls = mockFetch({ 'GET /v1/source-items/held': [] })
    const { container } = render(<Held onQueued={() => {}} />)
    await waitFor(() => expect(calls.length).toBeGreaterThan(0))
    expect(container.innerHTML).toBe('')
  })

  it('ingests exactly the selected items through the typed client', async () => {
    const calls = mockFetch({
      'GET /v1/source-items/held': HELD,
      'POST /v1/source-items/integrate': { queued: 1, skipped: 0 },
    })
    const onQueued = vi.fn()
    render(<Held onQueued={onQueued} />)

    fireEvent.click(await screen.findByLabelText('Select Acme raises $20M Series A'))
    fireEvent.click(screen.getByRole('button', { name: 'Ingest 1 now' }))

    expect(await screen.findByRole('status')).toHaveProperty(
      'textContent',
      '1 queued for processing.',
    )
    const post = calls.find((call) => call.method === 'POST')
    expect(post).toEqual({
      url: '/v1/source-items/integrate',
      method: 'POST',
      body: { ids: ['si_a'] },
    })
    expect(onQueued).toHaveBeenCalledOnce()
  })

  it('dismisses only after asking, and spends nothing', async () => {
    const calls = mockFetch({
      'GET /v1/source-items/held': HELD,
      'POST /v1/source-items/dismiss': { dismissed: 2, skipped: 0 },
    })
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true)
    const onQueued = vi.fn()
    const onDismissed = vi.fn()
    render(<Held onQueued={onQueued} onDismissed={onDismissed} />)

    fireEvent.click(await screen.findByLabelText('Select all'))
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(confirm).toHaveBeenCalledWith('Dismiss 2 items? They will not be briefed.')
    expect(calls.some((call) => call.method === 'POST')).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(await screen.findByRole('status')).toHaveProperty('textContent', '2 dismissed.')
    const posts = calls.filter((call) => call.method === 'POST')
    expect(posts).toEqual([
      { url: '/v1/source-items/dismiss', method: 'POST', body: { ids: ['si_a', 'si_b'] } },
    ])
    expect(onQueued).not.toHaveBeenCalled()
    // But the app is told, so the sidebar's held count does not stay stale (motet#98).
    expect(onDismissed).toHaveBeenCalledOnce()
  })

  it('says so when the list cannot be read, rather than rendering an empty panel', async () => {
    mockFetch({})
    render(<Held onQueued={() => {}} />)
    expect(await screen.findByRole('alert')).toBeDefined()
  })
})

describe('SourceItemDetail', () => {
  it('shows a held item with an empty stage 2 and no news item', async () => {
    const held: Detail = {
      id: 'si_a',
      title: 'Acme raises $20M Series A',
      state: 'pending',
      status: 'held',
      pulled: PULLED,
      processed: [],
      news_items: [],
    }
    mockFetch({ 'GET /v1/source-items/si_a': held })
    render(<SourceItemDetail id="si_a" onClose={() => {}} />)

    expect(await screen.findByText(/Not processed yet — held/)).toBeDefined()
    expect(screen.getByText(/None yet/)).toBeDefined()
  })

  it('explains a merge with the decision dedup recorded', async () => {
    const merged: Detail = {
      id: 'si_a',
      title: 'Acme raises $20M Series A',
      state: 'integrated',
      status: 'done',
      pulled: PULLED,
      processed: [
        {
          step: 'dedup',
          status: 'done',
          job: null,
          finished_at: '2026-09-13T17:10:00Z',
          error: null,
          outcome: 'merged',
          decision: {
            relation: 'same_event',
            reason: 'One funding round, two write-ups.',
            candidate_id: 'ni_1',
            candidate_title: 'Acme closes its Series A',
            model: 'anthropic/claude-sonnet-5',
            basis: 'first_pass',
            title: 'Acme closes its Series A',
            summary: 'Two newsletters covered the round.',
            decided_at: '2026-09-13T17:10:00Z',
          },
          cost_recorded: false,
        },
      ],
      news_items: [
        {
          id: 'ni_1',
          title: 'Acme closes its Series A',
          summary: 'Two newsletters covered the round.',
          read: false,
          source_count: 2,
          position: 1,
        },
      ],
    }
    mockFetch({ 'GET /v1/source-items/si_a': merged })
    render(<SourceItemDetail id="si_a" onClose={() => {}} />)

    expect(await screen.findByText('One funding round, two write-ups.')).toBeDefined()
    expect(screen.getByText(/same story — merged/)).toBeDefined()
    expect(screen.getByText(/anthropic\/claude-sonnet-5/)).toBeDefined()
    expect(screen.getByText(/this one is #2, merged in/)).toBeDefined()
  })
})
