import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HeldSourceItem, IngestionItem, NewsItem, ProcessingStatus } from '../api/client'
import { Backlog } from './Backlog'

const NOW = '2026-09-12T18:00:00Z'

const HELD: HeldSourceItem[] = [
  {
    id: 'si_h1',
    title: 'OpenAI Releases GPT-6 Astra',
    source_id: 'src_gmail',
    source_kind: 'gmail',
    source_name: 'Gmail (owner@motet.test)',
    received_at: '2026-09-12T15:04:00Z',
    chars: 28_100,
    preview: 'OpenAI today…',
  },
  {
    id: 'si_h2',
    title: 'Inside a Software Factory',
    source_id: 'src_gmail',
    source_kind: 'gmail',
    source_name: 'Gmail (owner@motet.test)',
    received_at: '2026-09-12T16:04:00Z',
    chars: 23_400,
    preview: 'A factory…',
  },
]

const news = (id: string, title: string, read = false): NewsItem => ({
  id,
  title,
  summary: `${title}, summarised.`,
  source_item_ids: [`si_${id}`],
  sources: [{ id: `si_${id}`, title: `${title} (newsletter)` }],
  read,
  created_at: '2026-09-12T14:00:00Z',
})

const ITEMS: NewsItem[] = [
  news('a', 'Anthropic ships a payments push'),
  news('b', 'Nvidia sales chief profiled'),
  news('c', 'An older story, already heard', true),
]

const PROCESSING: ProcessingStatus = {
  now: NOW,
  worker_last_seen_at: '2026-09-12T17:59:50Z',
  queues: [],
  readiness: [],
}

/** The API still reports a held item as pending (proto/issues/09); the strip must not. */
const HELD_AS_PENDING: IngestionItem = {
  id: 'si_h1',
  title: 'OpenAI Releases GPT-6 Astra',
  state: 'pending',
  attempts: 0,
  max_attempts: 5,
  next_attempt_at: null,
  last_error: null,
  created_at: '2026-09-12T15:04:00Z',
  source_kind: 'gmail',
}

function mockFetch(routes: Record<string, unknown>) {
  const calls: { url: string; method: string; body: unknown }[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
      const key = Object.keys(routes)
        .sort((a, b) => b.length - a.length)
        .find((route) => url.includes(route))
      return {
        ok: key !== undefined,
        status: key === undefined ? 404 : 200,
        statusText: 'OK',
        json: async () => (key === undefined ? { detail: 'not found' } : routes[key]),
      } as Response
    }),
  )
  return calls
}

function renderBacklog(
  overrides: Partial<Parameters<typeof Backlog>[0]> = {},
  routes: Record<string, unknown> = {},
) {
  const calls = mockFetch({
    '/v1/source-items/held': HELD,
    '/v1/source-items/integrate': { queued: 2, skipped: 0 },
    '/read': { ...ITEMS[0], read: true },
    ...routes,
  })
  const onChanged = vi.fn()
  const onOpenEpisode = vi.fn()
  render(
    <Backlog
      items={ITEMS}
      ingestion={[]}
      ingestionUnavailable={false}
      processing={PROCESSING}
      onChanged={onChanged}
      onOpenEpisode={onOpenEpisode}
      {...overrides}
    />,
  )
  return { calls, onChanged, onOpenEpisode }
}

beforeEach(() => {
  window.localStorage.setItem('motet.apiToken', 'test-token')
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Backlog', () => {
  it('summarises the three counts and splits the lists', async () => {
    renderBacklog()
    expect(await screen.findByText('OpenAI Releases GPT-6 Astra')).toBeDefined()
    const counts = screen.getByText('waiting for you').closest('p')!
    expect(counts.textContent).toMatch(/2 waiting for you/)
    expect(counts.textContent).toMatch(/2 ready to brief/)
    expect(counts.textContent).toMatch(/1 read/)

    const waiting = screen.getByRole('region', { name: 'Waiting for you' })
    expect(within(waiting).getByText('Inside a Software Factory')).toBeDefined()
    const ready = screen.getByRole('region', { name: 'Ready to brief' })
    expect(within(ready).getByText('Anthropic ships a payments push')).toBeDefined()
    expect(within(ready).queryByText('An older story, already heard')).toBeNull()
    // The read item is folded, not gone.
    expect(screen.getByText('An older story, already heard')).toBeDefined()
  })

  it('searches both lists at once and says what it is hiding', async () => {
    renderBacklog()
    await screen.findByText('OpenAI Releases GPT-6 Astra')
    fireEvent.change(screen.getByLabelText('Search titles'), { target: { value: 'factory' } })
    expect(screen.queryByText('OpenAI Releases GPT-6 Astra')).toBeNull()
    expect(screen.getByText('Inside a Software Factory')).toBeDefined()
    expect(screen.getByText('1 of 2')).toBeDefined()
    expect(screen.getByText(/No unread titles match/)).toBeDefined()
  })

  it('ingests a selection of held items through the bar', async () => {
    const { calls, onChanged } = renderBacklog()
    await screen.findByText('OpenAI Releases GPT-6 Astra')
    expect(screen.queryByRole('region', { name: 'Selection' })).toBeNull()

    fireEvent.click(screen.getByLabelText('Select all in Waiting for you'))
    const bar = screen.getByRole('region', { name: 'Selection' })
    expect(bar.textContent).toMatch(/2 selected/)
    fireEvent.click(within(bar).getByRole('button', { name: 'Ingest 2' }))

    await waitFor(() => {
      const post = calls.find((call) => call.url.includes('/v1/source-items/integrate'))
      expect(post?.method).toBe('POST')
      expect([...(post?.body as { ids: string[] }).ids].sort()).toEqual(['si_h1', 'si_h2'])
    })
    expect(await screen.findByText('2 queued for processing.')).toBeDefined()
    expect(onChanged).toHaveBeenCalled()
    expect(screen.queryByRole('region', { name: 'Selection' })).toBeNull()
  })

  it('marks a selection of news items read, one request each', async () => {
    const { calls, onChanged } = renderBacklog()
    await screen.findByText('OpenAI Releases GPT-6 Astra')
    fireEvent.click(screen.getByLabelText('Select Anthropic ships a payments push'))
    fireEvent.click(screen.getByLabelText('Select Nvidia sales chief profiled'))
    const bar = screen.getByRole('region', { name: 'Selection' })
    fireEvent.click(within(bar).getByRole('button', { name: 'Mark 2 read' }))
    await waitFor(() => {
      const reads = calls.filter((call) => call.url.includes('/read'))
      expect(reads.map((call) => /news-items\/(\w+)\/read/.exec(call.url)?.[1]).sort()).toEqual(['a', 'b'])
      expect(reads.every((call) => (call.body as { read: boolean }).read)).toBe(true)
    })
    expect(onChanged).toHaveBeenCalled()
  })

  it('selecting in the other list starts a fresh selection', async () => {
    renderBacklog()
    await screen.findByText('OpenAI Releases GPT-6 Astra')
    fireEvent.click(screen.getByLabelText('Select OpenAI Releases GPT-6 Astra'))
    fireEvent.click(screen.getByLabelText('Select Anthropic ships a payments push'))
    const bar = screen.getByRole('region', { name: 'Selection' })
    expect(bar.textContent).toMatch(/1 selected/)
    expect(within(bar).getByRole('button', { name: 'Mark 1 read' })).toBeDefined()
    expect((screen.getByLabelText('Select OpenAI Releases GPT-6 Astra') as HTMLInputElement).checked).toBe(false)
  })

  it('does not count a held item as processing, whatever the ingestion route says', async () => {
    renderBacklog({ ingestion: [HELD_AS_PENDING] })
    await screen.findByText('OpenAI Releases GPT-6 Astra')
    const strip = screen.getByRole('status')
    expect(strip.textContent).toMatch(/Worker running/)
    expect(strip.textContent).not.toMatch(/processing/)
  })

  it('opens the lifecycle drawer from a held row, and closes on Escape', async () => {
    renderBacklog(
      {},
      {
        '/v1/source-items/si_h1': {
          id: 'si_h1',
          title: 'OpenAI Releases GPT-6 Astra',
          state: 'pending',
          pulled: {
            source_id: 'src_gmail',
            source_kind: 'gmail',
            source_name: 'Gmail (owner@motet.test)',
            external_id: 'msg1',
            received_at: '2026-09-12T15:04:00Z',
            chars: 28_100,
            text: 'OpenAI today…',
            raw_stored: false,
          },
          processed: {
            status: 'held',
            job: null,
            integrated_at: null,
            error: null,
            outcome: null,
            title: null,
            summary: null,
            decision_recorded: false,
            cost_recorded: false,
          },
          news_items: [],
        },
      },
    )
    fireEvent.click(await screen.findByRole('button', { name: 'OpenAI Releases GPT-6 Astra' }))
    const drawer = await screen.findByRole('dialog', { name: 'Source item' })
    expect(await within(drawer).findByText(/waiting for you/)).toBeDefined()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: 'Source item' })).toBeNull()
  })

  it('makes an episode with the cap from the popover', async () => {
    const { calls, onOpenEpisode } = renderBacklog({}, { '/v1/episodes': { id: 'ep_1' } })
    await screen.findByText('OpenAI Releases GPT-6 Astra')
    fireEvent.click(screen.getByRole('button', { name: /Episode cap/ }))
    fireEvent.change(screen.getByLabelText('Cap (minutes)'), { target: { value: '7' } })
    fireEvent.click(screen.getByRole('button', { name: 'Make an episode' }))
    await waitFor(() => {
      const post = calls.find((call) => call.url.endsWith('/v1/episodes'))
      expect(post?.body).toMatchObject({ max_duration_ms: 7 * 60_000 })
    })
    expect(onOpenEpisode).toHaveBeenCalled()
  })
})
