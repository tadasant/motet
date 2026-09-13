import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Episode } from '../api/client'
import { Episodes, forgetLastShownEpisode, formatClock, listenState } from './Episodes'

/** A finished episode nobody has played. Same shape as App.test.tsx's `EPISODE`. */
const UNLISTENED: Episode = {
  id: 'ep_unlistened',
  title: 'Wednesday briefing',
  state: 'ready',
  duration_ms: 1_865_000, // 31:05
  max_duration_ms: 1_200_000,
  audio_bytes: 51_244,
  audio_media_type: 'audio/mpeg',
  last_error: null,
  created_at: '2026-08-26T07:00:00Z',
  published_at: '2026-08-26T07:05:00Z',
  listened_through_ms: 0,
  segments: [
    {
      news_item_id: 'ni_1',
      news_item_title: 'Acme raises $20M Series A',
      text: 'Acme raised twenty million dollars.',
      start_ms: 0,
      duration_ms: 1_865_000,
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

/** Played to 12:30 of 31:05 — the row the progress bar exists for. */
const IN_PROGRESS_EP: Episode = {
  ...UNLISTENED,
  id: 'ep_partway',
  title: 'Tuesday briefing',
  created_at: '2026-08-25T07:00:00Z',
  published_at: '2026-08-25T07:05:00Z',
  listened_through_ms: 750_000,
  segments: [UNLISTENED.segments[0]!, { ...UNLISTENED.segments[0]!, news_item_id: 'ni_2' }],
}

/** Heard to within the slack of the end. */
const LISTENED: Episode = {
  ...UNLISTENED,
  id: 'ep_done',
  title: 'Monday briefing',
  created_at: '2026-08-24T07:00:00Z',
  published_at: '2026-08-24T07:05:00Z',
  listened_through_ms: 1_862_000,
}

/** Still in the pipeline: no duration, no audio, nothing to have listened to. */
const RENDERING: Episode = {
  ...UNLISTENED,
  id: 'ep_rendering',
  title: 'Just now',
  state: 'rendering',
  duration_ms: 0,
  audio_bytes: null,
  audio_media_type: null,
  published_at: null,
  created_at: '2026-08-27T07:00:00Z',
}

const ALL = [LISTENED, UNLISTENED, IN_PROGRESS_EP]

function mockApi(list: Episode[] = ALL) {
  const calls: { url: string; method: string; body: unknown }[] = []
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
    let payload: unknown = { detail: 'not found' }
    let ok = true
    if (method === 'GET' && url.endsWith('/v1/episodes')) payload = list
    else if (url.endsWith('/v1/feed')) payload = { url: 'https://example.test/feed.xml?token=s', token: 's' }
    else if (url.endsWith('/listened')) payload = { episode_id: 'x', news_items_marked_read: 1 }
    else if (url.endsWith('/progress')) {
      payload = { episode_id: 'x', listened_through_ms: 1_865_000, news_items_marked_read: 1 }
    } else ok = false
    return { ok, status: ok ? 200 : 404, statusText: 'OK', json: async () => payload } as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return calls
}

function renderEpisodes(selected: Episode = UNLISTENED, list: Episode[] = ALL) {
  const onSelectEpisode = vi.fn()
  const onEpisodeChanged = vi.fn()
  const onBacklogChanged = vi.fn()
  const view = render(
    <Episodes
      episode={selected}
      episodes={list}
      processing={null}
      onEpisodeChanged={onEpisodeChanged}
      onSelectEpisode={onSelectEpisode}
      onBacklogChanged={onBacklogChanged}
    />,
  )
  return { ...view, onSelectEpisode, onEpisodeChanged, onBacklogChanged }
}

beforeEach(() => {
  forgetLastShownEpisode()
  window.localStorage.setItem('motet.apiToken', 'test-token')
})

afterEach(() => {
  vi.unstubAllGlobals()
  window.localStorage.clear()
})

describe('listenState', () => {
  it('reads the three states off listened_through_ms against duration_ms', () => {
    expect(listenState(UNLISTENED)).toBe('unlistened')
    expect(listenState(IN_PROGRESS_EP)).toBe('in_progress')
    expect(listenState(LISTENED)).toBe('listened')
    // Within the slack of the end counts as the end: nobody sits through the sign-off.
    expect(listenState({ duration_ms: 60_000, listened_through_ms: 55_000 })).toBe('listened')
    expect(listenState({ duration_ms: 60_000, listened_through_ms: 54_999 })).toBe('in_progress')
    // No duration yet means nothing to have heard, whatever the position says.
    expect(listenState(RENDERING)).toBe('unlistened')
  })

  it('formats a clock the way a player does', () => {
    expect(formatClock(750_000)).toBe('12:30')
    expect(formatClock(1_865_000)).toBe('31:05')
    expect(formatClock(3_725_000)).toBe('1:02:05')
    expect(formatClock(0)).toBe('0:00')
  })
})

describe('the shelf', () => {
  it('groups unlistened and in-progress under Up next and the rest under Listened, newest first', () => {
    mockApi()
    renderEpisodes()

    const upNext = screen.getByRole('region', { name: /up next/i })
    const rows = within(upNext).getAllByRole('listitem')
    expect(rows.map((row) => row.querySelector('.episode-title')?.textContent)).toEqual([
      'Wednesday briefing',
      'Tuesday briefing',
    ])
    expect(within(rows[0]!).getByText('Unlistened')).toBeTruthy()
    expect(within(rows[1]!).getByText('In progress')).toBeTruthy()

    // The listened group is its own disclosure, open because there are few of them.
    const listenedSummary = screen.getByText(/^Listened/, { selector: 'h3' })
    const details = listenedSummary.closest('details')
    expect(details?.open).toBe(true)
    expect(within(details!).getByText('Monday briefing')).toBeTruthy()
    expect(within(details!).getByText('Listened', { selector: '.badge' })).toBeTruthy()
    // A listened row offers no "Mark listened".
    expect(within(details!).queryByText('Mark listened')).toBeNull()
  })

  it('shows a thin progress bar and "12:30 of 31:05" on the in-progress row', () => {
    mockApi()
    renderEpisodes()
    const bar = screen.getByRole('progressbar')
    expect(bar.getAttribute('aria-valuenow')).toBe('750000')
    expect(bar.getAttribute('aria-valuemax')).toBe('1865000')
    expect(screen.getByText('12:30 of 31:05')).toBeTruthy()
    // Duration and story count on every ready row.
    expect(screen.getAllByText(/31:05 · 2 stories/)).toHaveLength(1)
    expect(screen.getAllByText(/31:05 · 1 story/)).toHaveLength(2)
    // The in-progress row's play affordance says Resume.
    expect(screen.getByRole('button', { name: 'Play Tuesday briefing' }).textContent).toContain('Resume')
  })

  it('folds the Listened group away once there are more than five', () => {
    const many = Array.from({ length: 6 }, (_, i) => ({
      ...LISTENED,
      id: `done_${i}`,
      title: `Old ${i}`,
    }))
    mockApi([UNLISTENED, ...many])
    renderEpisodes(UNLISTENED, [UNLISTENED, ...many])
    const details = screen.getByText(/^Listened/, { selector: 'h3' }).closest('details')
    expect(details?.open).toBe(false)
    expect(screen.getByText('(6)')).toBeTruthy()
  })

  it('badges an episode still in the pipeline as Working… and a failed one as Failed', () => {
    const failed: Episode = {
      ...RENDERING,
      id: 'ep_failed',
      title: 'Broke',
      state: 'failed',
      last_error: 'script: model returned no segments',
    }
    mockApi([RENDERING, failed])
    // Selected: the finished one. A first mount whose selected episode is still in the
    // pipeline opens on that episode's detail instead (see below).
    renderEpisodes(UNLISTENED, [UNLISTENED, RENDERING, failed])
    const working = screen.getByText('Just now').closest('li')!
    expect(within(working).getByText('Working…')).toBeTruthy()
    expect(within(working).getByText(/rendering/)).toBeTruthy()
    const broke = screen.getByText('Broke').closest('li')!
    expect(within(broke).getByText('Failed')).toBeTruthy()
    expect(within(broke).getByText('script: model returned no segments')).toBeTruthy()
    // Neither can be played; only the finished one has a Play pill.
    expect(within(working).queryByRole('button', { name: /^Play / })).toBeNull()
    expect(within(broke).queryByRole('button', { name: /^Play / })).toBeNull()
    expect(screen.getAllByRole('button', { name: /^Play / })).toHaveLength(1)
  })

  it('shows the empty state when there is nothing', () => {
    // App.tsx never mounts this with no episode, but the list arm still has to say
    // something sensible if the list it merges is empty apart from the selected one.
    mockApi([])
    renderEpisodes(UNLISTENED, [UNLISTENED])
    expect(screen.getByText('Wednesday briefing')).toBeTruthy()
  })
})

describe('clicking in and back', () => {
  it('opens the detail on click, selects it upstream, and comes back on the back link', async () => {
    mockApi()
    const { onSelectEpisode } = renderEpisodes(UNLISTENED)
    fireEvent.click(screen.getByRole('button', { name: 'Play Tuesday briefing' }))
    expect(onSelectEpisode).toHaveBeenCalledWith(IN_PROGRESS_EP)
    // The detail is EpisodeScreen, unchanged: its heading and its "Other episodes" line.
    expect(await screen.findByRole('heading', { name: 'Episode' })).toBeTruthy()
    expect(screen.queryByRole('heading', { name: 'Episodes' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: '← All episodes' }))
    expect(screen.getByRole('heading', { name: 'Episodes' })).toBeTruthy()
    expect(screen.getByRole('region', { name: /up next/i })).toBeTruthy()
  })

  it('lands on the detail when a *new* episode is selected from outside — the backlog just made one', () => {
    mockApi()
    const { rerender } = renderEpisodes(UNLISTENED)
    expect(screen.getByRole('heading', { name: 'Episodes' })).toBeTruthy()
    rerender(
      <Episodes
        episode={RENDERING}
        episodes={[RENDERING, ...ALL]}
        processing={null}
        onEpisodeChanged={vi.fn()}
        onSelectEpisode={vi.fn()}
        onBacklogChanged={vi.fn()}
      />,
    )
    expect(screen.getByRole('heading', { name: 'Episode' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '← All episodes' })).toBeTruthy()
  })

  it('opens on the detail when nothing is remembered and the selected episode is still being made', () => {
    // A fresh page load whose first visit here is "Make an episode" from the Backlog: no
    // remembered id to compare against, so the pipeline state is the tiebreak — a create
    // answers `pending`, and the Working… copy and the polling live on the detail.
    mockApi()
    renderEpisodes(RENDERING, [RENDERING, ...ALL])
    expect(screen.getByRole('heading', { name: 'Episode' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '← All episodes' })).toBeTruthy()
  })

  it('remembers across a remount: same episode lands on the list, a different one on the detail', () => {
    mockApi()
    const first = renderEpisodes(UNLISTENED)
    expect(screen.getByRole('heading', { name: 'Episodes' })).toBeTruthy()
    first.unmount()
    // Back to the tab, nothing changed: the list.
    const second = renderEpisodes(UNLISTENED)
    expect(screen.getByRole('heading', { name: 'Episodes' })).toBeTruthy()
    second.unmount()
    // Back to the tab because the backlog made one: the detail.
    renderEpisodes(RENDERING, [RENDERING, ...ALL])
    expect(screen.getByRole('heading', { name: 'Episode' })).toBeTruthy()
  })
})

describe('Mark listened', () => {
  it('writes read state and the end position, refreshes the list, and tells the backlog', async () => {
    const calls = mockApi()
    const { onBacklogChanged } = renderEpisodes()
    const row = screen.getByText('Wednesday briefing').closest('li')!
    fireEvent.click(within(row).getByRole('button', { name: 'Mark listened' }))

    await waitFor(() => expect(onBacklogChanged).toHaveBeenCalled())
    const posts = calls.filter((call) => call.method === 'POST')
    expect(posts.map((call) => call.url.replace(/^.*\/v1/, '/v1'))).toEqual([
      '/v1/episodes/ep_unlistened/listened',
      '/v1/episodes/ep_unlistened/progress',
    ])
    expect(posts[1]!.body).toEqual({ listened_through_ms: 1_865_000 })
    // The list re-asks rather than trusting itself.
    const lists = calls.filter((call) => call.method === 'GET' && call.url.endsWith('/v1/episodes'))
    await waitFor(() => expect(lists.length).toBeGreaterThanOrEqual(2))
  })
})
