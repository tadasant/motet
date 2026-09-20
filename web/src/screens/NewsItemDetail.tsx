// A story's provenance: what it says, and which write-ups it was made from.
//
// A backlog row names a story with one line, and for a merged one that line is dedup's —
// written by a model, about several newsletters at once. This screen is the answer to
// "says who": dedup's title and summary at the top, then every contributing source item
// with its own untouched title, when it arrived, and the opening of its text.
//
// One request (`GET /v1/news-items/{id}`) rather than one per source, and a *preview*
// rather than whole bodies: the full text and the pipeline behind it are one more click
// in, on the lifecycle drawer this screen embeds.

import { useEffect, useState } from 'react'

import { type NewsItemDetail as Detail, api } from '../api/client'
import { SourceItemDetail } from './SourceItemDetail'

function arrived(iso: string): string {
  return new Date(iso).toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

export function NewsItemDetail({
  id,
  onBack,
  onChanged,
}: {
  id: string
  onBack: () => void
  /** Read state was written here: the backlog list behind this screen is now stale. */
  onChanged: () => void
}) {
  const [data, setData] = useState<Detail | null>(null)
  const [error, setError] = useState('')
  const [openSource, setOpenSource] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setData(null)
    setError('')
    api
      .newsItem(id)
      .then((next) => {
        if (!cancelled) setData(next)
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err))
      })
    return () => {
      cancelled = true
    }
  }, [id])

  const toggleRead = async () => {
    if (!data) return
    try {
      await api.setRead(data.id, !data.read)
      setData({ ...data, read: !data.read })
      onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  return (
    <section aria-label="Story" className="news-detail">
      <nav className="back-row" aria-label="Backlog navigation">
        <button type="button" className="linkish" onClick={onBack}>
          ← Backlog
        </button>
      </nav>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {!data ? (
        !error && <p className="hint">Loading…</p>
      ) : (
        <>
          <h2 className="news-detail-title">{data.display_title || data.title}</h2>
          <p className="summary">{data.summary}</p>
          <div className="row news-detail-actions">
            <button type="button" onClick={toggleRead}>
              {data.read ? 'Mark unread' : 'Mark read'}
            </button>
            <span className="hint">
              {data.sources.length} source{data.sources.length === 1 ? '' : 's'}
            </span>
          </div>

          <h3>
            {/* Named for what it answers rather than for the table it comes from: this is
                where the story came from, in the order dedup accumulated it. */}
            {data.sources.length === 1 ? 'Source' : 'Sources'}
          </h3>
          <ul className="news-sources">
            {data.sources.map((source) => (
              <li key={source.id}>
                <strong>{source.title || '(untitled)'}</strong>
                <p className="hint">
                  {source.source_name} · {arrived(source.received_at)} ·{' '}
                  {source.chars.toLocaleString()} chars
                  {data.sources.length > 1 &&
                    (source.position === 0 ? ' · started this story' : ' · merged in')}
                </p>
                <blockquote className="source-preview">{source.preview}</blockquote>
                <button
                  type="button"
                  className="linkish hint"
                  aria-expanded={openSource === source.id}
                  onClick={() => setOpenSource(openSource === source.id ? null : source.id)}
                >
                  {openSource === source.id ? 'Hide full text and pipeline' : 'Full text and pipeline'}
                </button>
                {openSource === source.id && (
                  <SourceItemDetail id={source.id} onClose={() => setOpenSource(null)} />
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  )
}
