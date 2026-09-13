// Screen 2: the backlog, and the button that turns it into an episode.
//
// Read state is per news item (invariant 5) and this toggle writes the same column that
// "I listened to this episode" does — so marking something read here and having heard it
// on a walk are one fact, not two that drift.

import { useEffect, useState } from 'react'

import {
  ApiError,
  type Episode,
  type IngestionItem,
  type NewsItem,
  type ProcessingStatus,
  api,
} from '../api/client'
import { Motif } from '../brand/Brand'
import { Held } from './Held'
import { Processing } from './Processing'
import { SourceItemDetail } from './SourceItemDetail'

const DEFAULT_MAX_MINUTES = 20

export function Backlog({
  items,
  ingestion,
  ingestionUnavailable,
  processing,
  onChanged,
  onOpenEpisode,
}: {
  items: NewsItem[]
  ingestion: IngestionItem[]
  ingestionUnavailable: boolean
  processing: ProcessingStatus | null
  onChanged: () => void
  onOpenEpisode: (episode: Episode) => void
}) {
  const [minutes, setMinutes] = useState(DEFAULT_MAX_MINUTES)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  // Which source item's three-stage detail is open under a news item, and which news
  // item is being pointed at (scrolled to and briefly highlighted) from one.
  const [openSource, setOpenSource] = useState<string | null>(null)
  const [flash, setFlash] = useState<string | null>(null)

  useEffect(() => {
    if (!flash) return
    document.getElementById(`ni-${flash}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    const timer = window.setTimeout(() => setFlash(null), 2_500)
    return () => window.clearTimeout(timer)
  }, [flash])

  const unread = items.filter((item) => !item.read)

  const toggle = async (item: NewsItem) => {
    setError('')
    try {
      await api.setRead(item.id, !item.read)
      onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    }
  }

  const makeEpisode = async () => {
    setBusy(true)
    setError('')
    try {
      const episode = await api.createEpisode(
        `Episode — ${new Date().toLocaleDateString()}`,
        minutes * 60_000,
      )
      onOpenEpisode(episode)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section aria-label="Backlog">
      {/* Above the backlog rather than below it: the question "where did the thing I just
          pasted go" is asked immediately after pasting, and an answer under a long list of
          older stories is an answer nobody scrolls to. It renders nothing when there is
          nothing in flight. */}
      {/* What has been pulled in but not yet paid for (motet#91). */}
      <Held onQueued={onChanged} onDismissed={onChanged} onJumpToNewsItem={setFlash} />

      <Processing items={ingestion} unavailable={ingestionUnavailable} processing={processing} />

      <h3>Processed</h3>
      <p className="hint">
        {unread.length} unread of {items.length}. An episode takes everything unread, oldest
        first, until it hits the cap.
      </p>

      <div className="row">
        <label htmlFor="episode-minutes">Cap (minutes)</label>
        <input
          id="episode-minutes"
          type="number"
          min={1}
          max={120}
          value={minutes}
          onChange={(e) => setMinutes(Math.max(1, Number(e.target.value) || 1))}
        />
        <button type="button" className="primary" onClick={makeEpisode} disabled={busy || unread.length === 0}>
          {busy ? 'Creating…' : 'Make an episode'}
        </button>
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {items.length === 0 ? (
        // Only when nothing is in flight either — "nothing here yet" over the top of an
        // item that is visibly being retried is the same lie in a smaller font.
        ingestion.length === 0 &&
        !ingestionUnavailable && (
          // The one empty state that gets the motif (brand/GUIDELINES.md): what this list
          // becomes once there is something in it.
          <div className="empty">
            <p className="hint">
              Nothing here yet. Paste in a newsletter you trust, or connect a mailbox under
              Sources and pick what to ingest.
            </p>
            <Motif quiet caption="Many voices, one podcast." />
          </div>
        )
      ) : (
        <ul className="items">
          {items.map((item) => (
            <li
              key={item.id}
              id={`ni-${item.id}`}
              className={[item.read ? 'read' : '', flash === item.id ? 'flash' : '']
                .filter(Boolean)
                .join(' ')}
            >
              <div className="item-head">
                <strong>{item.title}</strong>
                <button type="button" onClick={() => toggle(item)}>
                  {item.read ? 'Mark unread' : 'Mark read'}
                </button>
              </div>
              <p>{item.summary}</p>
              {/* The source items behind this story, by title; each opens its
                  three-stage lifecycle. */}
              <p className="hint">
                {item.sources.length} source{item.sources.length === 1 ? '' : 's'}:{' '}
                {item.sources.map((source, index) => (
                  <span key={source.id}>
                    {index > 0 && ', '}
                    <button
                      type="button"
                      className="linkish hint"
                      aria-expanded={openSource === source.id}
                      onClick={() => setOpenSource(openSource === source.id ? null : source.id)}
                    >
                      {source.title || source.id}
                    </button>
                  </span>
                ))}{' '}
                · {item.id}
              </p>
              {openSource !== null && item.source_item_ids.includes(openSource) && (
                <SourceItemDetail
                  id={openSource}
                  onClose={() => setOpenSource(null)}
                  onJumpToNewsItem={setFlash}
                />
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
