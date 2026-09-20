// Renaming an episode, from the one control both surfaces that show a title use.

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Episode } from '../api/client'
import { EpisodeTitle } from './EpisodeTitle'

const EPISODE: Episode = {
  id: 'ep_1',
  // What an episode nobody named is called: the day it was made, composed by the server.
  title: '2026-09-20',
  state: 'ready',
  duration_ms: 92_000,
  max_duration_ms: 1_200_000,
  audio_bytes: 51_244,
  audio_media_type: 'audio/mpeg',
  last_error: null,
  created_at: '2026-09-20T07:00:00Z',
  published_at: '2026-09-20T07:05:00Z',
  listened_through_ms: 0,
  keep_in_backlog: false,
  segments: [],
}

type Call = { url: string; method: string; body: unknown }

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
        json: async () => (found ? routes[key] : { detail: 'No such episode.' }),
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

describe('renaming an episode', () => {
  it('sends the typed title, trimmed, to the title resource', async () => {
    const renamed = { ...EPISODE, title: 'The Tuesday walk' }
    const calls = mockFetch({ 'PUT /v1/episodes/ep_1/title': renamed })
    const onRenamed = vi.fn()
    render(
      <EpisodeTitle episode={EPISODE} onRenamed={onRenamed}>
        <span>{EPISODE.title}</span>
      </EpisodeTitle>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Rename 2026-09-20' }))
    fireEvent.change(screen.getByLabelText('Episode title'), {
      target: { value: '  The Tuesday walk  ' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(onRenamed).toHaveBeenCalledWith(renamed))
    expect(calls).toEqual([
      {
        url: '/v1/episodes/ep_1/title',
        method: 'PUT',
        body: { title: 'The Tuesday walk' },
      },
    ])
  })

  it('writes nothing for a blank title or an unchanged one', async () => {
    const calls = mockFetch({ 'PUT /v1/episodes/ep_1/title': EPISODE })
    render(
      <EpisodeTitle episode={EPISODE} onRenamed={() => {}}>
        <span>{EPISODE.title}</span>
      </EpisodeTitle>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Rename 2026-09-20' }))
    fireEvent.change(screen.getByLabelText('Episode title'), { target: { value: '   ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    // Blank is not a request to be re-named after the date: that default is applied once,
    // at creation, and undoing a rename is a rename.
    await waitFor(() => expect(screen.getByRole('button', { name: /Rename/ })).toBeDefined())
    expect(calls).toEqual([])
  })

  it('says why a refused rename failed and keeps the editor open', async () => {
    mockFetch({})
    render(
      <EpisodeTitle episode={EPISODE} onRenamed={() => {}}>
        <span>{EPISODE.title}</span>
      </EpisodeTitle>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Rename 2026-09-20' }))
    fireEvent.change(screen.getByLabelText('Episode title'), { target: { value: 'Nope' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect((await screen.findByRole('alert')).textContent).toMatch(/No such episode\./)
    expect(screen.getByLabelText('Episode title')).toBeDefined()
  })

  it('does not leave an alert on the row after a failure is cancelled', async () => {
    mockFetch({})
    render(
      <EpisodeTitle episode={EPISODE} onRenamed={() => {}}>
        <span>{EPISODE.title}</span>
      </EpisodeTitle>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Rename 2026-09-20' }))
    fireEvent.change(screen.getByLabelText('Episode title'), { target: { value: 'Nope' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await screen.findByRole('alert')

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    // The message belonged to an attempt that is over, and the row renders it too — left
    // set it would be announced beside the Rename button until the row unmounted.
    await waitFor(() => expect(screen.getByRole('button', { name: /Rename/ })).toBeDefined())
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('cancels on Escape without writing', async () => {
    const calls = mockFetch({ 'PUT /v1/episodes/ep_1/title': EPISODE })
    render(
      <EpisodeTitle episode={EPISODE} onRenamed={() => {}}>
        <span>{EPISODE.title}</span>
      </EpisodeTitle>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Rename 2026-09-20' }))
    fireEvent.keyDown(screen.getByLabelText('Episode title'), { key: 'Escape' })

    await waitFor(() => expect(screen.getByRole('button', { name: /Rename/ })).toBeDefined())
    expect(calls).toEqual([])
  })
})
