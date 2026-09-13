import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AdminWaitlist as Page } from '../api/client'
import { AdminWaitlist } from './AdminWaitlist'

const NEWEST: Page = {
  total: 3,
  signups: [
    {
      id: 3,
      email: 'third@example.com',
      created_at: '2026-09-13T09:00:00Z',
      last_submitted_at: '2026-09-13T09:00:00Z',
      submissions: 1,
    },
    {
      id: 2,
      email: 'second@example.com',
      created_at: '2026-09-12T09:00:00Z',
      last_submitted_at: '2026-09-13T08:00:00Z',
      submissions: 2,
    },
  ],
  next_before: 2,
}

const OLDER: Page = {
  total: 3,
  signups: [
    {
      id: 1,
      email: 'first@example.com',
      created_at: '2026-09-11T09:00:00Z',
      last_submitted_at: '2026-09-11T09:00:00Z',
      submissions: 1,
    },
  ],
  next_before: null,
}

/** A fetch that answers by path and query, and remembers what it was asked. */
function mockFetch(routes: Record<string, unknown>, status = 200): string[] {
  const urls: string[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), 'http://api.test')
      const key = `${url.pathname}${url.search}`
      urls.push(key)
      const found = key in routes
      const code = found ? status : 404
      return {
        ok: code < 400,
        status: code,
        statusText: 'status',
        json: async () => (found && code < 400 ? routes[key] : { detail: 'Refused.' }),
      } as Response
    }),
  )
  return urls
}

beforeEach(() => {
  window.localStorage.setItem('motet.apiToken', 'test-token')
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('AdminWaitlist', () => {
  it('lists the newest signups and pages to older ones', async () => {
    const urls = mockFetch({
      '/v1/admin/waitlist': NEWEST,
      '/v1/admin/waitlist?before=2': OLDER,
    })
    render(<AdminWaitlist />)

    expect(await screen.findByText('third@example.com')).toBeDefined()
    expect(screen.getByText('second@example.com')).toBeDefined()
    expect(screen.getByText('3 addresses')).toBeDefined()

    fireEvent.click(screen.getByRole('button', { name: 'Older signups' }))
    expect(await screen.findByText('first@example.com')).toBeDefined()
    expect(screen.queryByText('third@example.com')).toBeNull()
    expect(urls).toContain('/v1/admin/waitlist?before=2')
    expect(screen.getByRole('button', { name: 'Older signups' })).toHaveProperty('disabled', true)
  })

  it('says so when nobody has joined', async () => {
    mockFetch({ '/v1/admin/waitlist': { total: 0, signups: [], next_before: null } })
    render(<AdminWaitlist />)
    expect(await screen.findByText('nobody has joined yet')).toBeDefined()
  })

  it('shows the refusal rather than an empty list', async () => {
    mockFetch({ '/v1/admin/waitlist': NEWEST }, 403)
    render(<AdminWaitlist />)
    await waitFor(() => expect(screen.getByText(/Refused/)).toBeDefined())
    expect(screen.queryByText('third@example.com')).toBeNull()
  })
})
