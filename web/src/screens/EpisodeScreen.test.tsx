import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Episode } from '../api/client'
import { COULD_NOT_PLAY, EpisodeScreen } from './EpisodeScreen'

/** 31:05 long, two segments, played to 12:30. */
const EPISODE: Episode = {
  id: 'ep_1',
  title: 'Tuesday briefing',
  state: 'ready',
  duration_ms: 1_865_000,
  max_duration_ms: 1_200_000,
  audio_bytes: 51_244,
  audio_media_type: 'audio/mpeg',
  last_error: null,
  created_at: '2026-08-25T07:00:00Z',
  published_at: '2026-08-25T07:05:00Z',
  listened_through_ms: 750_000,
  keep_in_backlog: false,
  segments: [
    {
      news_item_id: 'ni_1',
      news_item_title: 'Acme raises $20M Series A',
      text: 'Acme raised twenty million dollars.',
      start_ms: 0,
      duration_ms: 900_000,
      claims: [],
    },
    {
      news_item_id: 'ni_2',
      news_item_title: 'Globex ships',
      text: 'Globex shipped.',
      start_ms: 900_000,
      duration_ms: 965_000,
      claims: [],
    },
  ],
}

/** What the audio route answers a diagnosis: a redirect by default, as a deployed API does. */
function mockApi(audio: { status: number; detail?: string } = { status: 0 }) {
  const calls: { url: string; method: string; body: unknown }[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : undefined
      calls.push({ url, method, body })
      let payload: unknown = { detail: 'not found' }
      let ok = true
      if (url.includes('/audio?token=')) {
        // `redirect: 'manual'` turns the 307 into an opaque response with status 0.
        return { ok: false, status: audio.status, json: async () => ({ detail: audio.detail }) } as Response
      }
      if (url.endsWith('/v1/feed')) {
        payload = { url: 'https://example.test/feed.xml?token=f33d', token: 'f33d' }
      } else if (url.endsWith('/listened')) {
        payload = { episode_id: 'ep_1', news_items_marked_read: 2 }
      } else if (url.endsWith('/position')) {
        // The server answers with where it now says the listener is — its own monotonic
        // copy, which is what the screen should believe.
        payload = {
          episode_id: 'ep_1',
          listened_through_ms: body?.listened_through_ms,
          news_items_marked_read: (body?.listened_through_ms as number) >= 900_000 ? 1 : 0,
        }
      } else ok = false
      return { ok, status: ok ? 200 : 404, statusText: 'OK', json: async () => payload } as Response
    }),
  )
  return calls
}

const positionWrites = (calls: ReturnType<typeof mockApi>) =>
  calls.filter((call) => call.method === 'PUT' && call.url.endsWith('/position'))

function renderScreen(episode: Episode = EPISODE, autoPlay = false) {
  const onPositionReported = vi.fn()
  const onBacklogChanged = vi.fn()
  const view = render(
    <EpisodeScreen
      episode={episode}
      processing={null}
      autoPlay={autoPlay}
      onPositionReported={onPositionReported}
      onBacklogChanged={onBacklogChanged}
    />,
  )
  return { ...view, onPositionReported, onBacklogChanged }
}

/** The player, once the feed token it needs has arrived. */
async function findAudio(): Promise<HTMLAudioElement> {
  return waitFor(() => {
    const el = document.querySelector('audio')
    expect(el).not.toBeNull()
    return el!
  })
}

/** jsdom has no media pipeline, so a test says where the playhead is and whether it runs. */
function setPlayhead(el: HTMLAudioElement, ms: number, paused: boolean) {
  Object.defineProperty(el, 'paused', { configurable: true, get: () => paused })
  el.currentTime = ms / 1000
}

/** Play from one point to another the way a browser reports it: a tick every `stepMs`. */
function playThrough(el: HTMLAudioElement, fromMs: number, toMs: number, stepMs = 1_000) {
  for (let at = fromMs; at <= toMs; at += stepMs) {
    setPlayhead(el, at, false)
    fireEvent.timeUpdate(el)
  }
}

beforeEach(() => {
  window.localStorage.setItem('motet.apiToken', 'test-token')
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  window.localStorage.clear()
})

describe('the in-page player', () => {
  it('points the element at the audio route with the feed token, not at a fetched blob', async () => {
    // A deployed API answers this route with a 307 to a signed URL on the object store's
    // origin. A media element follows that without CORS; a fetch() into a blob would need
    // the bucket to allow this origin, and nothing does.
    const calls = mockApi()
    renderScreen()
    const audio = await findAudio()
    expect(audio.getAttribute('src')).toBe('/v1/episodes/ep_1/audio?token=f33d')
    expect(calls.some((call) => call.url.includes('/audio'))).toBe(false)
  })

  it('resumes where the listener got to, and starts only when asked', async () => {
    mockApi()
    const play = vi.mocked(HTMLMediaElement.prototype.play)
    renderScreen()
    const audio = await findAudio()
    fireEvent(audio, new Event('loadedmetadata'))
    expect(audio.currentTime).toBe(750)
    expect(screen.getByText('Resumes at 12:30, where you got to.')).toBeTruthy()
    expect(play).not.toHaveBeenCalled()
  })

  it('plays on open when the shelf asked it to', async () => {
    mockApi()
    const play = vi.mocked(HTMLMediaElement.prototype.play)
    renderScreen(EPISODE, true)
    fireEvent(await findAudio(), new Event('loadedmetadata'))
    expect(play).toHaveBeenCalledTimes(1)
  })

  it('starts a listened episode from the top rather than at its end', async () => {
    mockApi()
    renderScreen({ ...EPISODE, listened_through_ms: EPISODE.duration_ms })
    const audio = await findAudio()
    audio.currentTime = 0
    fireEvent(audio, new Event('loadedmetadata'))
    expect(audio.currentTime).toBe(0)
    expect(screen.queryByText(/Resumes at/)).toBeNull()
  })

  it('reports the position as playback passes the reporting step, and tells the app', async () => {
    const calls = mockApi()
    const { onPositionReported, onBacklogChanged } = renderScreen()
    const audio = await findAudio()

    // Nine seconds on from where the server already is: nothing to say yet.
    playThrough(audio, 750_000, 759_000)
    expect(positionWrites(calls)).toHaveLength(0)

    // A step further: reported, from the frontier the server holds.
    playThrough(audio, 760_000, 760_000)
    await waitFor(() => expect(positionWrites(calls)).toHaveLength(1))
    expect(positionWrites(calls)[0]!.body).toEqual({ listened_through_ms: 760_000 })
    await waitFor(() => expect(onPositionReported).toHaveBeenCalledWith('ep_1', 760_000))

    // On past the end of the first story: the backlog is told, because it became read.
    playThrough(audio, 761_000, 905_000)
    await waitFor(() => expect(onBacklogChanged).toHaveBeenCalled())
    expect(positionWrites(calls).at(-1)!.body).toEqual({ listened_through_ms: 900_000 })
  })

  it('does not count scrubbing: a position reached while paused is never reported', async () => {
    // Otherwise dragging the scrubber to the end to see how long it is would mark every
    // story in the episode read.
    const calls = mockApi()
    const { unmount } = renderScreen()
    const audio = await findAudio()
    setPlayhead(audio, 1_800_000, true)
    fireEvent.timeUpdate(audio)
    fireEvent.pause(audio)
    unmount()
    expect(positionWrites(calls)).toHaveLength(0)
  })

  it('does not count the end of a file somebody scrubbed to while paused', async () => {
    // A seek to the very end runs the element's "reached the end" steps, `ended` included,
    // with nothing having played. Reporting the duration there would mark every story
    // read and pin the row to Listened for good — both one-way.
    const calls = mockApi()
    renderScreen()
    const audio = await findAudio()
    setPlayhead(audio, 1_865_000, true)
    fireEvent.timeUpdate(audio)
    fireEvent(audio, new Event('ended'))
    await act(async () => undefined)
    expect(positionWrites(calls)).toHaveLength(0)
  })

  it('does not count a jump ahead, even when the listener plays on from there', async () => {
    // The server marks every story the position has passed, so reporting 15:30 after a
    // ▶ jump to the last story would mark the skipped one read too.
    const calls = mockApi()
    const { unmount } = renderScreen()
    const audio = await findAudio()
    playThrough(audio, 750_000, 752_000)
    fireEvent.click(screen.getByRole('button', { name: 'Play from 15:00: Globex ships' }))
    fireEvent(audio, new Event('seeking'))
    playThrough(audio, 900_000, 930_000)
    fireEvent.pause(audio)
    unmount()
    // Only what was heard at the frontier before the jump, and that is under one step.
    await act(async () => undefined)
    expect(positionWrites(calls).map((call) => call.body)).toEqual([{ listened_through_ms: 752_000 }])
  })

  it('flushes what was played on pause and on leaving the screen', async () => {
    const calls = mockApi()
    const { unmount } = renderScreen()
    const audio = await findAudio()

    playThrough(audio, 750_000, 753_000)
    fireEvent.pause(audio)
    await waitFor(() => expect(positionWrites(calls)).toHaveLength(1))
    expect(positionWrites(calls)[0]!.body).toEqual({ listened_through_ms: 753_000 })

    playThrough(audio, 753_000, 756_000)
    unmount()
    await waitFor(() => expect(positionWrites(calls)).toHaveLength(2))
    expect(positionWrites(calls)[1]!.body).toEqual({ listened_through_ms: 756_000 })
  })

  it('reports the whole duration when playback runs out from the frontier', async () => {
    const calls = mockApi()
    renderScreen({ ...EPISODE, listened_through_ms: 1_850_000 })
    const audio = await findAudio()
    playThrough(audio, 1_850_000, 1_864_000)
    fireEvent.pause(audio)
    fireEvent(audio, new Event('ended'))
    await waitFor(() =>
      expect(positionWrites(calls).at(-1)!.body).toEqual({ listened_through_ms: 1_865_000 }),
    )
  })

  it('does not retry a refused report on every tick', async () => {
    // A rollback-and-retry would send several requests a second for as long as the API
    // said no — an expired session, an outage. The next report covers a lost one.
    const calls = mockApi()
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const url = String(input)
      calls.push({ url, method: init?.method ?? 'GET', body: undefined })
      if (url.endsWith('/v1/feed')) {
        return { ok: true, status: 200, json: async () => ({ url: 'u', token: 'f33d' }) } as Response
      }
      return { ok: false, status: 401, statusText: 'Unauthorized', json: async () => ({}) } as Response
    })
    renderScreen()
    const audio = await findAudio()
    // A tick at a time, letting the refusal land in between, as a browser would.
    for (let at = 750_000; at <= 765_000; at += 250) {
      playThrough(audio, at, at)
      await act(async () => undefined)
    }
    expect(positionWrites(calls)).toHaveLength(1)
  })

  it('stops reporting what the server already holds once it moves under the player', async () => {
    // Mark listened, or another device: the position is already heard and already told.
    const calls = mockApi()
    const { rerender } = renderScreen()
    const audio = await findAudio()
    rerender(
      <EpisodeScreen
        episode={{ ...EPISODE, listened_through_ms: EPISODE.duration_ms }}
        processing={null}
        onPositionReported={vi.fn()}
        onBacklogChanged={vi.fn()}
      />,
    )
    playThrough(audio, 750_000, 770_000)
    fireEvent.pause(audio)
    await act(async () => undefined)
    expect(positionWrites(calls)).toHaveLength(0)
  })

  it('seeks to a segment and plays from there', async () => {
    mockApi()
    const play = vi.mocked(HTMLMediaElement.prototype.play)
    renderScreen()
    const audio = await findAudio()
    fireEvent.click(screen.getByRole('button', { name: 'Play from 15:00: Globex ships' }))
    expect(audio.currentTime).toBe(900)
    expect(play).toHaveBeenCalledTimes(1)
  })

  it('offers no player, and no seek buttons, before the episode is ready', async () => {
    const calls = mockApi()
    renderScreen({ ...EPISODE, state: 'rendering', duration_ms: 0, listened_through_ms: 0 })
    await waitFor(() => expect(calls.some((call) => call.url.endsWith('/v1/feed'))).toBe(true))
    await act(async () => undefined)
    expect(document.querySelector('audio')).toBeNull()
    expect(screen.queryByRole('button', { name: /^Play from/ })).toBeNull()
    expect(screen.getByText(/starts at 15:00/)).toBeTruthy()
  })
})

describe('Mark listened on the detail', () => {
  it('writes the same two facts the shelf does, so the row moves to Listened too', async () => {
    const calls = mockApi()
    const { onPositionReported, onBacklogChanged } = renderScreen()
    fireEvent.click(screen.getByRole('button', { name: 'Mark listened' }))

    expect(await screen.findByText('2 news items marked read.')).toBeTruthy()
    const writes = calls.filter((call) => call.method !== 'GET')
    expect(writes.map((call) => `${call.method} ${call.url}`)).toEqual([
      'POST /v1/episodes/ep_1/listened',
      'PUT /v1/episodes/ep_1/position',
    ])
    expect(writes[1]!.body).toEqual({ listened_through_ms: 1_865_000 })
    expect(onPositionReported).toHaveBeenCalledWith('ep_1', 1_865_000)
    expect(onBacklogChanged).toHaveBeenCalled()
  })
})

describe('the player transport (motet#110)', () => {
  it('plays and pauses the element from the ink play circle', async () => {
    mockApi()
    renderScreen()
    const audio = await findAudio()
    const pause = vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => undefined)

    Object.defineProperty(audio, 'paused', { configurable: true, get: () => true })
    fireEvent.click(screen.getByRole('button', { name: 'Play' }))
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalled()

    // The circle follows the element's own events, not the click.
    fireEvent.play(audio)
    Object.defineProperty(audio, 'paused', { configurable: true, get: () => false })
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))
    expect(pause).toHaveBeenCalled()
  })

  it('steps the playback rate from the speed pill', async () => {
    mockApi()
    renderScreen()
    const audio = await findAudio()

    fireEvent.click(screen.getByRole('button', { name: 'Playback speed 1×' }))
    expect(audio.playbackRate).toBe(1.2)
    expect(screen.getByRole('button', { name: 'Playback speed 1.2×' }).textContent).toBe('1.2×')
  })

  it('seeks from the scrubber without claiming anything was heard', async () => {
    const calls = mockApi()
    renderScreen()
    const audio = await findAudio()

    fireEvent.change(screen.getByRole('slider', { name: 'Seek' }), { target: { value: '1500000' } })
    expect(audio.currentTime).toBe(1500)
    // The browser echoes a seek; playing on from past the frontier is not having heard it.
    fireEvent(audio, new Event('seeking'))
    playThrough(audio, 1_500_000, 1_520_000)
    fireEvent.pause(audio)
    expect(positionWrites(calls)).toHaveLength(0)
  })

  it('says so when the audio cannot load, rather than leaving a play button that does nothing', async () => {
    const calls = mockApi()
    renderScreen()
    const audio = await findAudio()

    fireEvent.play(audio)
    fireEvent.error(audio)
    expect((await screen.findByRole('alert')).textContent).toBe(COULD_NOT_PLAY)
    expect(screen.getByRole('button', { name: 'Play' })).toBeTruthy()
    // The route was asked why, and answered with a redirect: the file is there.
    await waitFor(() => expect(calls.some((call) => call.url.includes('/audio?token=f33d'))).toBe(true))
    expect(screen.getByRole('alert').textContent).toBe(COULD_NOT_PLAY)
  })

  it('says the feed link changed when the route refuses its token, rather than blaming the browser', async () => {
    mockApi({ status: 401 })
    renderScreen()
    fireEvent.error(await findAudio())
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('feed link has changed'))
  })

  it("shows the API's reason when the audio is gone from storage, not a browser fault", async () => {
    // Staging's bucket deletes audio after its retention window; the route says 410.
    mockApi({ status: 410, detail: "This episode's audio is no longer in storage." })
    renderScreen()
    const audio = await findAudio()

    fireEvent.error(audio)
    await waitFor(() =>
      expect(screen.getByRole('alert').textContent).toBe("This episode's audio is no longer in storage."),
    )
    expect(screen.getByRole('alert').textContent).not.toContain('podcast feed')
  })

  it('draws Play Live as the mic pill inside the transport, once', async () => {
    mockApi()
    renderScreen()
    await findAudio()

    const transport = await screen.findByRole('group', { name: 'Player' })
    const pill = await waitFor(() => {
      const found = transport.querySelector('button.mic')
      expect(found).not.toBeNull()
      return found as HTMLButtonElement
    })
    expect(pill.textContent).toBe('Play Live')
    // No voice service answers in this test, so it is disabled — and not drawn twice.
    await waitFor(() => expect(pill.disabled).toBe(true))
    expect(screen.getAllByRole('button', { name: 'Play Live' })).toHaveLength(1)
  })
})
