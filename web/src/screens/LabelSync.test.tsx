import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { LabelSync as LabelSyncState, Source } from '../api/client'
import { LabelSync } from './LabelSync'

const OFF: LabelSyncState = {
  status: 'off',
  remove_label: null,
  add_label: null,
  modify_granted: false,
  available_labels: ['Completed', 'Newsletters', 'INBOX'],
  labels_read_at: '2026-09-13T04:00:00Z',
  last_synced_at: null,
  failed_items: 0,
  last_error: null,
}

const MAILBOX: Source = {
  id: 'src_gmail',
  kind: 'gmail',
  name: 'Gmail',
  active: true,
  connected: true,
  scopes: ['https://www.googleapis.com/auth/gmail.readonly'],
  last_polled_at: '2026-09-13T04:00:00Z',
  last_error: null,
  created_at: '2026-09-12T00:00:00Z',
  query: 'category:updates OR category:promotions',
  first_sync_days: null,
  last_sync: null,
  disconnected_at: null,
  items_pulled_in: 0,
  items_integrated: 0,
  label_sync: OFF,
}

const withSync = (sync: Partial<LabelSyncState>): Source => ({
  ...MAILBOX,
  label_sync: { ...OFF, ...sync },
})

type Call = { url: string; method: string; body: unknown }

function stubFetch(answer: unknown): Call[] {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({
        url: String(input),
        method: init?.method ?? 'GET',
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      })
      return { ok: true, status: 200, json: async () => answer } as Response
    }),
  )
  return calls
}

afterEach(() => {
  vi.unstubAllGlobals()
  window.sessionStorage.clear()
})

describe('label sync', () => {
  it('says plainly that an unconfigured mailbox is only read', () => {
    render(<LabelSync source={MAILBOX} onChange={vi.fn()} navigate={vi.fn()} />)
    expect(screen.getByText(/Off\. Motet only reads this mailbox/)).toBeDefined()
    // No consent is offered to a mailbox that has not chosen label sync: the wider scope
    // is only ever asked for because of a setting the owner made.
    expect(screen.queryByRole('button', { name: 'Re-authorize Gmail' })).toBeNull()
  })

  it('offers the mailbox labels as suggestions in both pickers', () => {
    const { container } = render(
      <LabelSync source={MAILBOX} onChange={vi.fn()} navigate={vi.fn()} />,
    )
    const options = [...container.querySelectorAll('datalist option')].map((o) =>
      o.getAttribute('value'),
    )
    expect(options).toEqual(['Completed', 'Newsletters', 'INBOX'])
    const remove = screen.getByLabelText('When I ingest an item, remove this label')
    const add = screen.getByLabelText('…and add this label')
    expect(remove.getAttribute('list')).toBe(add.getAttribute('list'))
  })

  it('saves both labels, sending an empty one as null', async () => {
    const saved = withSync({ status: 'needs_reauthorization', add_label: 'Completed' })
    const calls = stubFetch(saved)
    const onChange = vi.fn()
    render(<LabelSync source={MAILBOX} onChange={onChange} navigate={vi.fn()} />)

    fireEvent.change(screen.getByLabelText('…and add this label'), {
      target: { value: 'Completed' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save labels' }))

    await waitFor(() => expect(onChange).toHaveBeenCalledWith(saved))
    expect(calls).toHaveLength(1)
    expect(calls[0]?.method).toBe('PUT')
    expect(calls[0]?.url).toContain('/v1/sources/src_gmail/label-sync')
    expect(calls[0]?.body).toEqual({ remove_label: null, add_label: 'Completed' })
  })

  it('asks for re-authorization, and starts it only when the owner presses the button', async () => {
    const calls = stubFetch({
      source_id: 'src_gmail',
      authorization_url: 'https://accounts.google.test/o/oauth2/v2/auth?scope=modify',
      state: 'st_label',
    })
    const navigate = vi.fn()
    render(
      <LabelSync
        source={withSync({
          status: 'needs_reauthorization',
          remove_label: 'Newsletters',
          add_label: 'Completed',
        })}
        onChange={vi.fn()}
        navigate={navigate}
      />,
    )
    expect(screen.getByText('Needs re-authorization to enable label sync.')).toBeDefined()
    expect(calls).toHaveLength(0)

    fireEvent.click(screen.getByRole('button', { name: 'Re-authorize Gmail' }))

    await waitFor(() =>
      expect(navigate).toHaveBeenCalledWith(
        'https://accounts.google.test/o/oauth2/v2/auth?scope=modify',
      ),
    )
    expect(calls[0]?.method).toBe('POST')
    expect(calls[0]?.url).toContain('/v1/sources/src_gmail/reauthorize')
    expect(calls[0]?.body).toEqual({ redirect_uri: `${window.location.origin}/oauth/callback` })
    // Remembered before the redirect, exactly as a first connect does.
    expect(window.sessionStorage.getItem('motet.oauthState')).toBe('st_label')
  })

  it('names the move when it is on, and what the last attempts did', () => {
    render(
      <LabelSync
        source={withSync({
          status: 'on',
          remove_label: 'Newsletters',
          add_label: 'Completed',
          modify_granted: true,
          last_synced_at: '2026-09-13T04:10:00Z',
          failed_items: 2,
          last_error: 'Gmail is unavailable fetching a label change (503); retry later',
        })}
        onChange={vi.fn()}
        navigate={vi.fn()}
      />,
    )
    expect(screen.getByText(/moves its message from Newsletters to Completed/)).toBeDefined()
    expect(screen.getByText(/2 ingested items could not be moved/)).toBeDefined()
    expect(screen.getByText(/Gmail is unavailable/)).toBeDefined()
    expect(screen.queryByRole('button', { name: 'Re-authorize Gmail' })).toBeNull()
  })

  it('renders nothing for a source that is not a mailbox', () => {
    const { container } = render(
      <LabelSync
        source={{ ...MAILBOX, kind: 'paste', label_sync: null }}
        onChange={vi.fn()}
        navigate={vi.fn()}
      />,
    )
    expect(container.innerHTML).toBe('')
  })
})
