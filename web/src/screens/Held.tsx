// PROTOTYPE — "Ingested, awaiting processing".
//
// Connecting a source does the free, deterministic work (poll, fetch, extract) at once;
// inference is spent only when a person picks items here and says "ingest now". This
// panel is that step: every extracted source item with no integrate job yet, with a
// checkbox each, select-all, and one button.

import { Fragment, useCallback, useEffect, useMemo, useState } from 'react'

import { apiBaseUrl, getToken } from '../api/client'
import { SourceItemDetail } from './SourceItemDetail'

export type HeldItem = {
  id: string
  title: string
  source_id: string
  source_kind: string
  source_name: string
  received_at: string
  chars: number
  preview: string
}

const POLL_MS = 5_000

function authHeaders(): Record<string, string> {
  const token = getToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

async function fetchHeld(): Promise<HeldItem[]> {
  const response = await fetch(`${apiBaseUrl()}/v1/source-items/held`, {
    headers: authHeaders(),
  })
  if (!response.ok) throw new Error(`GET /v1/source-items/held → ${response.status}`)
  return (await response.json()) as HeldItem[]
}

async function integrate(ids: string[]): Promise<{ queued: number; skipped: number }> {
  const response = await fetch(`${apiBaseUrl()}/v1/source-items/integrate`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ ids }),
  })
  if (!response.ok) throw new Error(`POST /v1/source-items/integrate → ${response.status}`)
  return (await response.json()) as { queued: number; skipped: number }
}

function when(iso: string): string {
  const d = new Date(iso)
  const sameDay = d.toDateString() === new Date().toDateString()
  return sameDay
    ? d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
    : d.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

export function Held({
  onQueued,
  onJumpToNewsItem,
}: {
  onQueued: () => void
  onJumpToNewsItem?: ((newsItemId: string) => void) | undefined
}) {
  const [items, setItems] = useState<HeldItem[] | null>(null)
  const [selected, setSelected] = useState<Set<string>>(() => new Set())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [open, setOpen] = useState<string | null>(null)

  const refresh = useCallback(() => {
    fetchHeld()
      .then((next) => {
        setItems(next)
        // Drop selections for items that are no longer held.
        setSelected((prev) => {
          const ids = new Set(next.map((item) => item.id))
          return new Set([...prev].filter((id) => ids.has(id)))
        })
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)))
  }, [])

  useEffect(() => {
    refresh()
    const timer = window.setInterval(refresh, POLL_MS)
    return () => window.clearInterval(timer)
  }, [refresh])

  const all = items ?? []
  const allSelected = all.length > 0 && all.every((item) => selected.has(item.id))
  const someSelected = selected.size > 0
  const selectedChars = useMemo(
    () => all.filter((item) => selected.has(item.id)).reduce((sum, item) => sum + item.chars, 0),
    [all, selected],
  )

  const toggleAll = () => {
    setSelected(allSelected ? new Set() : new Set(all.map((item) => item.id)))
  }
  const toggleOne = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const ingestNow = async () => {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const result = await integrate([...selected])
      setNotice(
        `${result.queued} queued for processing${result.skipped ? ` (${result.skipped} skipped)` : ''}.`,
      )
      setSelected(new Set())
      refresh()
      onQueued()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  if (items !== null && items.length === 0 && !error) return null

  return (
    <div className="held">
      <div className="row">
        <h3>
          Ingested, awaiting processing <span className="tab-count">{all.length}</span>
        </h3>
        <button type="button" onClick={ingestNow} disabled={busy || !someSelected}>
          {busy ? 'Queueing…' : someSelected ? `Ingest ${selected.size} now` : 'Ingest now'}
        </button>
        {someSelected && (
          <span className="hint">
            ~{Math.round(selectedChars / 1000)}k chars → dedup at low effort, each
          </span>
        )}
        {notice && (
          <span className="ok" role="status">
            {notice}
          </span>
        )}
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <p className="hint">
        Pulled from your sources but not yet deduped into the backlog. Nothing here has cost
        inference. Pick what you want briefed.
      </p>
      {all.length > 0 && (
        <table className="grid held-table">
          <thead>
            <tr>
              <th className="check">
                <input
                  type="checkbox"
                  aria-label="Select all"
                  checked={allSelected}
                  onChange={toggleAll}
                />
              </th>
              <th>title</th>
              <th>source</th>
              <th>received</th>
              <th className="num">size</th>
            </tr>
          </thead>
          <tbody>
            {all.map((item) => (
              <Fragment key={item.id}>
                <tr
                  className={selected.has(item.id) ? 'selected' : ''}
                  onClick={() => toggleOne(item.id)}
                >
                  <td className="check" onClick={(e) => e.stopPropagation()}>
                    <input
                      type="checkbox"
                      aria-label={`Select ${item.title}`}
                      checked={selected.has(item.id)}
                      onChange={() => toggleOne(item.id)}
                    />
                  </td>
                  <td>
                    <strong title={item.preview}>{item.title || '(untitled)'}</strong>{' '}
                    <button
                      type="button"
                      className="linkish hint"
                      onClick={(e) => {
                        e.stopPropagation()
                        setOpen(open === item.id ? null : item.id)
                      }}
                    >
                      {open === item.id ? 'hide' : 'details'}
                    </button>
                  </td>
                  <td className="hint">{item.source_name}</td>
                  <td className="hint" title={item.received_at}>
                    {when(item.received_at)}
                  </td>
                  <td className="num hint">{(item.chars / 1000).toFixed(1)}k</td>
                </tr>
                {open === item.id && (
                  <tr className="preview-row">
                    <td />
                    <td colSpan={4} onClick={(e) => e.stopPropagation()}>
                      {/* The three-stage drill-down; for a held item stages 2 and 3 are
                          deliberately empty boxes rather than absent ones. */}
                      <SourceItemDetail
                        id={item.id}
                        onClose={() => setOpen(null)}
                        onJumpToNewsItem={onJumpToNewsItem}
                      />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
