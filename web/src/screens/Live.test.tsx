import { render, screen } from '@testing-library/react'
import { createRef } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api, type Episode } from '../api/client'
import { Live, rmsDbfs } from './Live'

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

afterEach(() => {
  vi.restoreAllMocks()
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

describe('rmsDbfs', () => {
  it('reads a full-scale sine near -3 dBFS and silence as the floor', () => {
    const sine = new Float32Array(1600).map((_, i) => Math.sin((2 * Math.PI * 300 * i) / 16_000))
    expect(rmsDbfs(sine)).toBeCloseTo(-3.01, 1)
    expect(rmsDbfs(new Float32Array(160))).toBe(-100)
    expect(rmsDbfs(new Float32Array(0))).toBe(-100)
  })
})
