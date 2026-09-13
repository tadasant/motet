// "Pulled in, waiting for you" — the gate between free work and paid work (motet#91).
//
// Connecting a source does the free, deterministic work (poll, fetch, extract) at once;
// inference is spent only when a person picks items here and says "ingest now". This
// panel is that step: every extracted source item with no integrate job yet, with a
// checkbox each, select-all, one button that spends and one that discards.

import { Fragment, useCallback, useEffect, useMemo, useState } from 'react'

import { type HeldSourceItem, api } from '../api/client'
import { SourceItemDetail } from './SourceItemDetail'

const POLL_MS = 5_000

/** The API's bound on the held list (`repo.HELD_MAX_ITEMS`); a full page may not be all. */
const HELD_PAGE = 500

function when(iso: string): string {
  const d = new Date(iso)
  const sameDay = d.toDateString() === new Date().toDateString()
  return sameDay
    ? d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
    : d.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

export function Held({
  onQueued,
  onJumpToNewsItem,
}: {
  onQueued: () => void
  onJumpToNewsItem?: ((newsItemId: string) => void) | undefined
}) {
  const [items, setItems] = useState<HeldSourceItem[] | null>(null)
  const [selected, setSelected] = useState<Set<string>>(() => new Set())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [open, setOpen] = useState<string | null>(null)

  const refresh = useCallback(() => {
    api
      .heldSourceItems()
      .then((next) => {
        setItems(next)
        setError('')
        // Drop selections for items that are no longer held.
        setSelected((prev) => {
          const ids = new Set(next.map((item) => item.id))
          return new Set([...prev].filter((id) => ids.has(id)))
        })
      })
      .catch((err: unknown) => setError(message(err)))
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

  const act = async (run: () => Promise<string>) => {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      setNotice(await run())
      setSelected(new Set())
      refresh()
    } catch (err) {
      setError(message(err))
    } finally {
      setBusy(false)
    }
  }

  const ingestNow = () =>
    act(async () => {
      const result = await api.integrateSourceItems([...selected])
      onQueued()
      return `${result.queued} queued for processing${result.skipped ? ` (${result.skipped} skipped)` : ''}.`
    })

  const dismiss = () => {
    // Nothing brings a dismissed item back, so this is the one click that asks first.
    const count = selected.size
    if (
      !window.confirm(`Dismiss ${count} item${count === 1 ? '' : 's'}? They will not be briefed.`)
    )
      return
    return act(async () => {
      const result = await api.dismissSourceItems([...selected])
      return `${result.dismissed} dismissed${result.skipped ? ` (${result.skipped} skipped)` : ''}.`
    })
  }

  // Hidden when nothing is held — including right after an action empties the list; the
  // Processing panel below is where ingested items show up next.
  if (items !== null && items.length === 0 && !error) return null

  return (
    <div className="held">
      <div className="row">
        <h3>
          Pulled in, waiting for you{' '}
          <span className="tab-count">
            {all.length >= HELD_PAGE ? `${HELD_PAGE}+` : all.length}
          </span>
        </h3>
        <button type="button" onClick={ingestNow} disabled={busy || !someSelected}>
          {busy ? 'Working…' : someSelected ? `Ingest ${selected.size} now` : 'Ingest now'}
        </button>
        <button type="button" onClick={dismiss} disabled={busy || !someSelected}>
          Dismiss
        </button>
        {someSelected && (
          <span className="hint">
            ~{(selectedChars / 1000).toFixed(1)}k chars → dedup at low effort, each
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
        inference. Pick what you want briefed; dismiss what you do not.
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
                      aria-expanded={open === item.id}
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
