// Screen 2: the backlog, and the button that turns it into an episode.
//
// Read state is per news item (invariant 5) and this toggle writes the same column that
// "I listened to this episode" does — so marking something read here and having heard it
// on a walk are one fact, not two that drift.
//
// **A row is named after its source, not after dedup.** One source: that newsletter's own
// subject line, verbatim, because it is what its reader recognises. Several: dedup's
// title, which is the only one that can name more than one write-up at once, with a badge
// saying how many. The server decides which (`display_title`); this screen renders it.
// Clicking a row opens its provenance rather than expanding a summary in place.

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
import { NewsItemDetail } from './NewsItemDetail'
import { Processing } from './Processing'

const DEFAULT_MAX_MINUTES = 20

function when(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

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
  // Which story's provenance is open, and which row is being pointed at (scrolled to and
  // briefly highlighted) from the held list or a lifecycle drawer.
  const [openItem, setOpenItem] = useState<string | null>(null)
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
      onOpenEpisode(await api.createEpisode(minutes * 60_000))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  // The provenance view replaces the list, the way an episode's detail replaces the
  // shelf: one section, one thing on screen, and Back is a link rather than a URL.
  if (openItem !== null) {
    return (
      <NewsItemDetail id={openItem} onBack={() => setOpenItem(null)} onChanged={onChanged} />
    )
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
        {unread.length} unread of {items.length}.
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
            <p className="hint">Nothing here yet. Paste in a newsletter, or connect a mailbox.</p>
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
                {/* The title is the link in: a keyboard and a screen reader reach the same
                    action a pointer does, so the row itself needs no role of its own. */}
                <button
                  type="button"
                  className="linkish news-title"
                  onClick={() => setOpenItem(item.id)}
                >
                  {item.display_title || item.title}
                </button>
                {item.sources.length > 1 && (
                  // The affordance for a merge: how many write-ups are behind this one
                  // line. One source needs no badge — the line *is* that source's title.
                  <span className="badge">{item.sources.length} sources</span>
                )}
                <button type="button" onClick={() => toggle(item)}>
                  {item.read ? 'Mark unread' : 'Mark read'}
                </button>
              </div>
              <p className="hint">{when(item.created_at)}</p>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
