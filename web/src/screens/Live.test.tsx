import { act, fireEvent, render, screen } from '@testing-library/react'
import { createRef } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api, type Episode, type VoiceSession } from '../api/client'
import { Live, primePlayback, rmsDbfs } from './Live'

const EPISODE: Episode = {
  id: 'ep_1',
  title: 'Morning briefing',
  state: 'ready',
  duration_ms: 60_000,
  max_duration_ms: 1_800_000,
  audio_bytes: 1000,
  audio_media_type: 'audio/mpeg',
  last_error: null,
  created_at: '2026-09-13T07:00:00Z',
  published_at: '2026-09-13T07:00:00Z',
  listened_through_ms: 0,
  segments: [],
}

// jsdom has no media pipeline; Play Live unlocks the player inside the tap, so every start
// calls these.
beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => undefined)
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('Play Live with no voice service configured', () => {
  it('disables the button, says why, and never reaches for a voice host', async () => {
    const status = vi
      .spyOn(api, 'voiceStatus')
      .mockResolvedValue({ configured: false, reason: "Live voice isn't configured in this environment." })
    const mint = vi.spyOn(api, 'startVoiceSession')
    const sockets = vi.fn()
    vi.stubGlobal('WebSocket', sockets)

    render(<Live episode={EPISODE} player={createRef<HTMLAudioElement>()} />)

    expect(await screen.findByText("Live voice isn't configured in this environment.")).toBeTruthy()
    const button = screen.getByRole('button', { name: 'Play Live' }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    expect(status).toHaveBeenCalledTimes(1)
    expect(mint).not.toHaveBeenCalled()
    expect(sockets).not.toHaveBeenCalled()
    vi.unstubAllGlobals()
  })

  it('treats an API that cannot answer the question as unavailable, not as a crash', async () => {
    vi.spyOn(api, 'voiceStatus').mockRejectedValue(new Error('GET /v1/voice → 404'))

    render(<Live episode={EPISODE} player={createRef<HTMLAudioElement>()} />)

    expect(await screen.findByText('Could not ask the API whether live voice is available.')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Play Live' }) as HTMLButtonElement).disabled).toBe(true)
  })
})

describe('Play Live with a voice service', () => {
  it('offers the button', async () => {
    vi.spyOn(api, 'voiceStatus').mockResolvedValue({ configured: true, reason: null })

    render(<Live episode={EPISODE} player={createRef<HTMLAudioElement>()} />)

    const button = (await screen.findByRole('button', { name: 'Play Live' })) as HTMLButtonElement
    await vi.waitFor(() => expect(button.disabled).toBe(false))
    expect(screen.getByText(/Headphones recommended/)).toBeTruthy()
  })
})

describe('Play Live drawn as the transport mic pill (motet#110)', () => {
  it('puts its one Play Live control in the slot, and starts a session from it', async () => {
    vi.spyOn(api, 'voiceStatus').mockResolvedValue({ configured: true, reason: null })
    const mint = vi.spyOn(api, 'startVoiceSession').mockRejectedValue(new Error('mint refused'))
    stubAudioStack([])
    const slot = document.createElement('span')
    document.body.appendChild(slot)
    const player = createRef<HTMLAudioElement>()

    render(
      <>
        <audio ref={player} />
        <Live episode={EPISODE} player={player} micSlot={slot} />
      </>,
    )

    const pill = await vi.waitFor(() => {
      const found = slot.querySelector('button.mic') as HTMLButtonElement | null
      expect(found?.disabled).toBe(false)
      return found!
    })
    expect(pill.textContent).toBe('Play Live')
    // Not drawn a second time in its own row.
    expect(screen.getAllByRole('button', { name: 'Play Live' })).toHaveLength(1)

    fireEvent.click(pill)
    await vi.waitFor(() => expect(mint).toHaveBeenCalledWith('ep_1', 0))
    expect(await screen.findByText('mint refused')).toBeTruthy()
    slot.remove()
  })
})

describe('Play Live stopped while it is still starting', () => {
  it('opens no socket and takes no mic once the mint finally answers', async () => {
    vi.spyOn(api, 'voiceStatus').mockResolvedValue({ configured: true, reason: null })
    let answer: (session: VoiceSession) => void = () => undefined
    vi.spyOn(api, 'startVoiceSession').mockReturnValue(
      new Promise<VoiceSession>((resolve) => {
        answer = resolve
      }),
    )
    const sockets = vi.fn()
    vi.stubGlobal('WebSocket', sockets)
    // A browser's audio stack, so an unguarded start would get as far as the mic and the socket.
    vi.stubGlobal(
      'AudioContext',
      class {
        sampleRate = 16_000
        destination = {}
        resume() {
          return Promise.resolve()
        }
        close() {
          return Promise.resolve()
        }
        createMediaStreamSource() {
          return { connect: () => undefined }
        }
        createScriptProcessor() {
          return { connect: () => undefined, disconnect: () => undefined, onaudioprocess: null }
        }
      },
    )
    const getUserMedia = vi.fn().mockResolvedValue({ getTracks: () => [] })
    Object.defineProperty(navigator, 'mediaDevices', { value: { getUserMedia }, configurable: true })
    const player = createRef<HTMLAudioElement>()

    render(
      <>
        <audio ref={player} />
        <Live episode={EPISODE} player={player} />
      </>,
    )
    const play = (await screen.findByRole('button', { name: 'Play Live' })) as HTMLButtonElement
    await vi.waitFor(() => expect(play.disabled).toBe(false))
    fireEvent.click(play)
    fireEvent.click(await screen.findByRole('button', { name: 'Stop Live' }))
    answer({
      session_id: 's',
      session_token: 't',
      expires_at: '0',
      websocket_url: 'ws://voice.invalid/v1/voice/sessions/s/stream',
      arm: 'openai_realtime',
      conversational: true,
      authenticate_frame: { type: 'authenticate' },
    })
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20))
    })

    expect(getUserMedia).not.toHaveBeenCalled()
    expect(sockets).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Play Live' })).toBeTruthy()
    vi.unstubAllGlobals()
  })
})

describe('rmsDbfs', () => {
  it('reads a full-scale sine near -3 dBFS and silence as the floor', () => {
    const sine = new Float32Array(1600).map((_, i) => Math.sin((2 * Math.PI * 300 * i) / 16_000))
    expect(rmsDbfs(sine)).toBeCloseTo(-3.01, 1)
    expect(rmsDbfs(new Float32Array(160))).toBe(-100)
    expect(rmsDbfs(new Float32Array(0))).toBe(-100)
  })
})

/** A browser audio stack that records what a tap was allowed to do before the first await. */
function stubAudioStack(log: string[]) {
  vi.stubGlobal(
    'AudioContext',
    class {
      sampleRate = 16_000
      destination = {}
      currentTime = 0
      constructor() {
        log.push('AudioContext')
      }
      resume() {
        log.push('resume')
        return Promise.resolve()
      }
      close() {
        return Promise.resolve()
      }
      createMediaStreamSource() {
        return { connect: () => undefined }
      }
      createScriptProcessor() {
        return { connect: () => undefined, disconnect: () => undefined, onaudioprocess: null }
      }
    },
  )
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [] }) },
    configurable: true,
  })
}

/** A socket the test drives: it opens at once and hands back what the page sent. */
class FakeSocket {
  static OPEN = 1
  static CLOSED = 3
  static CLOSING = 2
  static last: FakeSocket | null = null
  readyState = 1
  sent: Record<string, unknown>[] = []
  onopen: (() => void) | null = null
  onmessage: ((message: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  onclose: ((event: { code: number }) => void) | null = null
  constructor(readonly url: string) {
    FakeSocket.last = this
    setTimeout(() => this.onopen?.(), 0)
  }
  send(data: string) {
    this.sent.push(JSON.parse(data) as Record<string, unknown>)
  }
  close() {
    this.readyState = 3
  }
  deliver(event: Record<string, unknown>) {
    this.onmessage?.({ data: JSON.stringify(event) })
  }
}

const SESSION: VoiceSession = {
  session_id: 's',
  session_token: 't',
  expires_at: '0',
  websocket_url: 'ws://voice.invalid/v1/voice/sessions/s/stream',
  arm: 'openai_realtime',
  conversational: true,
  authenticate_frame: { type: 'authenticate' },
}

describe('Play Live on a browser that only allows audio inside a tap (iOS Safari)', () => {
  it('makes and resumes its AudioContext, and unlocks the player, before the mint is awaited', async () => {
    vi.spyOn(api, 'voiceStatus').mockResolvedValue({ configured: true, reason: null })
    const log: string[] = []
    // The mint never answers: whatever happened, happened inside the click.
    vi.spyOn(api, 'startVoiceSession').mockImplementation(() => {
      log.push('mint')
      return new Promise<VoiceSession>(() => undefined)
    })
    stubAudioStack(log)
    const play = vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(() => {
      log.push('play')
      return Promise.resolve()
    })
    vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {
      log.push('pause')
    })
    const player = createRef<HTMLAudioElement>()
    render(
      <>
        <audio ref={player} />
        <Live episode={EPISODE} player={player} />
      </>,
    )
    const button = (await screen.findByRole('button', { name: 'Play Live' })) as HTMLButtonElement
    await vi.waitFor(() => expect(button.disabled).toBe(false))

    fireEvent.click(button)

    expect(log).toEqual(['play', 'pause', 'AudioContext', 'resume', 'mint'])
    expect(play).toHaveBeenCalledTimes(1)
    vi.unstubAllGlobals()
  })

  it('does not pause a listener who is already playing to unlock the element', () => {
    const el = document.createElement('audio')
    Object.defineProperty(el, 'paused', { configurable: true, get: () => false })
    const play = vi.spyOn(el, 'play')
    const pause = vi.spyOn(el, 'pause').mockImplementation(() => undefined)
    primePlayback(el)
    expect(play).not.toHaveBeenCalled()
    expect(pause).not.toHaveBeenCalled()
  })

  it('tells the session narration is paused when the browser still refuses to start it', async () => {
    vi.spyOn(api, 'voiceStatus').mockResolvedValue({ configured: true, reason: null })
    vi.spyOn(api, 'startVoiceSession').mockResolvedValue(SESSION)
    stubAudioStack([])
    vi.stubGlobal('WebSocket', FakeSocket)
    const play = vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
    vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => undefined)
    const player = createRef<HTMLAudioElement>()
    render(
      <>
        <audio ref={player} />
        <Live episode={EPISODE} player={player} />
      </>,
    )
    const button = (await screen.findByRole('button', { name: 'Play Live' })) as HTMLButtonElement
    await vi.waitFor(() => expect(button.disabled).toBe(false))
    fireEvent.click(button)
    const socket = await vi.waitFor(() => {
      expect(FakeSocket.last?.sent[0]).toEqual({ type: 'authenticate' })
      return FakeSocket.last!
    })

    // The `ready` arrives over the socket, outside any tap, and this browser says no.
    play.mockRejectedValueOnce(new DOMException('not allowed', 'NotAllowedError'))
    await act(async () => {
      socket.deliver({ type: 'session_state', state: 'ready', live: true })
      await new Promise((resolve) => setTimeout(resolve, 0))
    })

    expect(socket.sent.map((frame) => frame.type)).toContain('narration_paused')
    expect(await screen.findByText('the browser would not start the narration — press play')).toBeTruthy()
    FakeSocket.last = null
    vi.unstubAllGlobals()
  })
})
