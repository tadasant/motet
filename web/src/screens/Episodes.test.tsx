import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Episode } from '../api/client'
import { Episodes } from './Episodes'
import { formatClock, listenState } from './listening'

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
  keep_in_backlog: false,
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
          start_ms: 0,
          duration_ms: 1_865_000,
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

function mockApi() {
  const calls: { url: string; method: string; body: unknown }[] = []
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
    let payload: unknown = { detail: 'not found' }
    let ok = true
    if (url.endsWith('/v1/feed')) payload = { url: 'https://example.test/feed.xml?token=s', token: 's' }
    else if (url.endsWith('/listened')) payload = { episode_id: 'x', news_items_marked_read: 1 }
    else if (url.endsWith('/position')) {
      payload = { episode_id: 'x', listened_through_ms: 1_865_000, news_items_marked_read: 1 }
    } else ok = false
    return { ok, status: ok ? 200 : 404, statusText: 'OK', json: async () => payload } as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return calls
}

/**
 * The section as App mounts it, with App's two pieces of state held by a stand-in: which
 * episode is open lives *above* this component, because the component unmounts whenever
 * another section is showing.
 */
function renderEpisodes({
  list = ALL,
  openId = null,
  loaded = true,
  unavailable = false,
}: { list?: Episode[]; openId?: string | null; loaded?: boolean; unavailable?: boolean } = {}) {
  const onOpen = vi.fn()
  const onBack = vi.fn()
  const onPositionReported = vi.fn()
  const onChanged = vi.fn()
  function Harness() {
    const [open, setOpen] = useState<string | null>(openId)
    return (
      <Episodes
        episodes={list}
        openId={open}
        loaded={loaded}
        unavailable={unavailable}
        onOpen={(episode) => {
          onOpen(episode)
          setOpen(episode.id)
        }}
        onBack={() => {
          onBack()
          setOpen(null)
        }}
        onPositionReported={onPositionReported}
        onChanged={onChanged}
      />
    )
  }
  const view = render(<Harness />)
  return { ...view, onOpen, onBack, onPositionReported, onChanged }
}

beforeEach(() => {
  window.localStorage.setItem('motet.apiToken', 'test-token')
  // jsdom has no media pipeline: `play()` is "not implemented" and returns nothing.
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  window.localStorage.clear()
})

describe('listenState', () => {
  it('reads the three states off listened_through_ms against duration_ms', () => {
    expect(listenState(UNLISTENED)).toBe('unlistened')
    expect(listenState(IN_PROGRESS_EP)).toBe('in_progress')
    expect(listenState(LISTENED)).toBe('listened')
    // Within the slack of the end counts as the end: nobody sits through the sign-off.
    const ready = { state: 'ready', duration_ms: 60_000 }
    expect(listenState({ ...ready, listened_through_ms: 55_000 })).toBe('listened')
    expect(listenState({ ...ready, listened_through_ms: 54_999 })).toBe('in_progress')
    // No duration yet means nothing to have heard, whatever the position says.
    expect(listenState(RENDERING)).toBe('unlistened')
    // Nor does an episode that is not ready, even one still carrying an old render's
    // duration and a position through it: that render no longer stands.
    expect(listenState({ ...LISTENED, state: 'failed' })).toBe('unlistened')
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
    // The in-progress row's pill says Resume, and its accessible name starts with that word.
    expect(screen.getByRole('button', { name: 'Resume Tuesday briefing' }).textContent).toContain('Resume')
  })

  it('folds the Listened group away once there are more than five', () => {
    const many = Array.from({ length: 6 }, (_, i) => ({
      ...LISTENED,
      id: `done_${i}`,
      title: `Old ${i}`,
    }))
    mockApi()
    renderEpisodes({ list: [UNLISTENED, ...many] })
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
    mockApi()
    renderEpisodes({ list: [UNLISTENED, RENDERING, failed] })
    // Both want attention, so both are under Up next rather than hidden.
    const upNext = screen.getByRole('region', { name: /up next/i })
    const working = within(upNext).getByText('Just now').closest('li')!
    expect(within(working).getByText('Working…')).toBeTruthy()
    expect(within(working).getByText(/rendering/)).toBeTruthy()
    const broke = within(upNext).getByText('Broke').closest('li')!
    expect(within(broke).getByText('Failed')).toBeTruthy()
    expect(within(broke).getByText('script: model returned no segments')).toBeTruthy()
    // Neither can be played; only the finished one has a Play pill.
    expect(within(working).queryByRole('button', { name: /^(Play|Resume) / })).toBeNull()
    expect(within(broke).queryByRole('button', { name: /^(Play|Resume) / })).toBeNull()
    expect(screen.getAllByRole('button', { name: /^(Play|Resume) / })).toHaveLength(1)
  })

  it('tells "none yet", "not looked yet" and "could not find out" apart', () => {
    mockApi()
    const first = renderEpisodes({ list: [], loaded: false })
    expect(screen.getByText('Looking for your episodes…')).toBeTruthy()
    first.unmount()
    const second = renderEpisodes({ list: [], unavailable: true })
    expect(screen.getByText(/Could not load your episodes/)).toBeTruthy()
    second.unmount()
    renderEpisodes({ list: [] })
    expect(screen.getByText('Make one from the backlog.')).toBeTruthy()
  })

  it('keeps showing what it has when a refresh fails, and says the list is stale', () => {
    mockApi()
    renderEpisodes({ unavailable: true })
    expect(screen.getByText('Wednesday briefing')).toBeTruthy()
    expect(screen.getByText(/Could not refresh/)).toBeTruthy()
  })
})

describe('clicking in and back', () => {
  it('opens the detail on a row click and comes back on the back link', async () => {
    mockApi()
    const { onOpen, onBack } = renderEpisodes()
    fireEvent.click(screen.getByText('Tuesday briefing').closest('li')!)
    expect(onOpen).toHaveBeenCalledWith(IN_PROGRESS_EP)
    expect(await screen.findByRole('region', { name: 'Episode' })).toBeTruthy()
    expect(screen.getByText('Tuesday briefing', { selector: 'strong' })).toBeTruthy()
    expect(screen.queryByRole('region', { name: 'Episodes' })).toBeNull()
    // The shelf replaced the detail's old "Other episodes:" line.
    expect(screen.queryByText(/Other episodes/)).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: '← All episodes' }))
    expect(onBack).toHaveBeenCalled()
    expect(screen.getByRole('region', { name: 'Episodes' })).toBeTruthy()
    expect(screen.getByRole('region', { name: /up next/i })).toBeTruthy()
  })

  it('opens once on a title click, not once for the button and again for the row', () => {
    mockApi()
    const { onOpen } = renderEpisodes()
    fireEvent.click(screen.getByRole('button', { name: 'Wednesday briefing' }))
    expect(onOpen).toHaveBeenCalledTimes(1)
  })

  it('renders the detail of whichever episode is open when it mounts', () => {
    // App holds the open id, so coming back to the section lands where it was left.
    mockApi()
    renderEpisodes({ list: [RENDERING, ...ALL], openId: RENDERING.id })
    expect(screen.getByRole('region', { name: 'Episode' })).toBeTruthy()
    expect(screen.getByText('Just now', { selector: 'strong' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '← All episodes' })).toBeTruthy()
  })

  it('starts the player from the Play pill, and a row click only opens', async () => {
    mockApi()
    const play = vi.mocked(HTMLMediaElement.prototype.play)
    renderEpisodes()

    fireEvent.click(screen.getByText('Wednesday briefing').closest('li')!)
    const quiet = await waitFor(() => {
      const el = document.querySelector('audio')
      expect(el).not.toBeNull()
      return el!
    })
    fireEvent(quiet, new Event('loadedmetadata'))
    expect(play).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '← All episodes' }))
    fireEvent.click(screen.getByRole('button', { name: 'Play Wednesday briefing' }))
    const audio = await waitFor(() => {
      const el = document.querySelector('audio')
      expect(el).not.toBeNull()
      return el!
    })
    fireEvent(audio, new Event('loadedmetadata'))
    expect(play).toHaveBeenCalledTimes(1)
  })
})

describe('Mark listened', () => {
  it('writes read state and the end position in that order, then moves the row and tells the app', async () => {
    const calls = mockApi()
    const { onChanged, onPositionReported } = renderEpisodes()
    const row = screen.getByText('Wednesday briefing').closest('li')!
    fireEvent.click(within(row).getByRole('button', { name: 'Mark listened' }))

    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    const writes = calls.filter((call) => call.method !== 'GET')
    expect(writes.map((call) => `${call.method} ${call.url.replace(/^.*\/v1/, '/v1')}`)).toEqual([
      'POST /v1/episodes/ep_unlistened/listened',
      'PUT /v1/episodes/ep_unlistened/position',
    ])
    expect(writes[1]!.body).toEqual({ listened_through_ms: 1_865_000 })
    // Moved at once, not on the refresh's answer.
    expect(onPositionReported).toHaveBeenCalledWith('ep_unlistened', 1_865_000)
    // A click on the row's action must not also open the row.
    expect(screen.getByRole('region', { name: 'Episodes' })).toBeTruthy()
  })

  it('says what went wrong when a write fails, and leaves the row where it was', async () => {
    const calls = mockApi()
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      calls.push({ url: String(input), method: init?.method ?? 'GET', body: undefined })
      return {
        ok: false,
        status: 500,
        statusText: 'Server Error',
        json: async () => ({ detail: 'database is away' }),
      } as Response
    })
    const { onChanged, onPositionReported } = renderEpisodes()
    const row = screen.getByText('Wednesday briefing').closest('li')!
    fireEvent.click(within(row).getByRole('button', { name: 'Mark listened' }))

    expect(await screen.findByText(/database is away/)).toBeTruthy()
    expect(onPositionReported).not.toHaveBeenCalled()
    expect(onChanged).not.toHaveBeenCalled()
    // The first write failed, so the second was never attempted.
    expect(calls.filter((call) => call.method === 'POST')).toHaveLength(1)
  })
})
