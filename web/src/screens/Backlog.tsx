// Screen 2: the backlog, and the button that turns it into an episode.
//
// Read state is per news item (invariant 5) and this toggle writes the same column that
// "I listened to this episode" does — so marking something read here and having heard it
// on a walk are one fact, not two that drift.

import { useState } from 'react'

import { ApiError, type Episode, type NewsItem, api } from '../api/client'

const DEFAULT_MAX_MINUTES = 20

export function Backlog({
  items,
  onChanged,
  onOpenEpisode,
}: {
  items: NewsItem[]
  onChanged: () => void
  onOpenEpisode: (episode: Episode) => void
}) {
  const [minutes, setMinutes] = useState(DEFAULT_MAX_MINUTES)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

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
        `Briefing — ${new Date().toLocaleDateString()}`,
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
    <section aria-labelledby="backlog-heading">
      <h2 id="backlog-heading">Backlog</h2>
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
        <button type="button" onClick={makeEpisode} disabled={busy || unread.length === 0}>
          {busy ? 'Creating…' : 'Make an episode'}
        </button>
      </div>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {items.length === 0 ? (
        <p className="hint">Nothing here yet. Paste a newsletter in.</p>
      ) : (
        <ul className="items">
          {items.map((item) => (
            <li key={item.id} className={item.read ? 'read' : ''}>
              <div className="item-head">
                <strong>{item.title}</strong>
                <button type="button" onClick={() => toggle(item)}>
                  {item.read ? 'Mark unread' : 'Mark read'}
                </button>
              </div>
              <p>{item.summary}</p>
              <p className="hint">
                {item.source_item_ids.length} source
                {item.source_item_ids.length === 1 ? '' : 's'} · {item.id}
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
