// Screen 3: the episode — transcript with each claim beside its source span.
//
// This screen is the product's argument that it is not making things up. Every spoken
// sentence is shown next to the verbatim source text it is answerable to (invariant 3),
// which is why the API resolves the span server-side rather than leaving the client to
// fetch sources and hope it bothers.
//
// There is deliberately no player here. Phase 1 ships a private RSS feed instead, because
// a browser cannot do background audio or offline and a dog walk needs both.

import { useEffect, useState } from 'react'

import { ApiError, type Episode, type FeedInfo, api } from '../api/client'

/** States a client should keep polling through. */
const IN_PROGRESS = new Set(['pending', 'scripting', 'rendering'])

const POLL_INTERVAL_MS = 2000

function formatDuration(ms: number): string {
  const total = Math.round(ms / 1000)
  const minutes = Math.floor(total / 60)
  const seconds = total % 60
  return `${minutes}:${String(seconds).padStart(2, '0')}`
}

export function EpisodeScreen({
  episode,
  onEpisodeChanged,
  onBacklogChanged,
}: {
  episode: Episode
  onEpisodeChanged: (episode: Episode) => void
  onBacklogChanged: () => void
}) {
  const [feed, setFeed] = useState<FeedInfo | null>(null)
  const [error, setError] = useState('')
  const [listened, setListened] = useState<number | null>(null)

  useEffect(() => {
    api.feed().then(setFeed).catch(() => setFeed(null))
  }, [])

  // Poll while the pipeline is still working. The episode moves pending -> scripting ->
  // rendering -> ready on the queue, so a client that fetched once would show "pending"
  // forever and look broken.
  useEffect(() => {
    if (!IN_PROGRESS.has(episode.state)) return
    const timer = setInterval(() => {
      api
        .episode(episode.id)
        .then(onEpisodeChanged)
        .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
    }, POLL_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [episode.id, episode.state, onEpisodeChanged])

  const markListened = async () => {
    try {
      const result = await api.markListened(episode.id)
      setListened(result.news_items_marked_read)
      onBacklogChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    }
  }

  return (
    <section aria-labelledby="episode-heading">
      <h2 id="episode-heading">Episode</h2>
      <p className="hint">
        <strong>{episode.title}</strong> · {episode.state}
        {episode.state === 'ready' && ` · ${formatDuration(episode.duration_ms)}`}
      </p>

      {IN_PROGRESS.has(episode.state) && (
        <p className="hint" role="status">
          Working… assembly, script, grounding validation, then audio. This page polls.
        </p>
      )}
      {episode.state === 'failed' && (
        <p className="error" role="alert">
          {episode.last_error ?? 'This episode failed.'}
        </p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {episode.state === 'ready' && (
        <div className="row">
          <button type="button" onClick={markListened}>
            Mark listened
          </button>
          {listened !== null && (
            <span className="ok" role="status">
              {listened} news item{listened === 1 ? '' : 's'} marked read.
            </span>
          )}
        </div>
      )}

      {feed && episode.state === 'ready' && (
        <p className="hint">
          Listen in a podcast app — paste this private feed URL into Overcast or Apple
          Podcasts: <code className="feed-url">{feed.url}</code>
        </p>
      )}

      {episode.segments.map((segment) => (
        <article key={segment.news_item_id} className="segment">
          <h3>{segment.news_item_title}</h3>
          <p className="hint">starts at {formatDuration(segment.start_ms)}</p>
          {segment.claims.length === 0 ? (
            <p className="hint">No claims yet — the script stage has not run.</p>
          ) : (
            <table className="claims">
              <thead>
                <tr>
                  <th scope="col">Spoken</th>
                  <th scope="col">Source span</th>
                </tr>
              </thead>
              <tbody>
                {segment.claims.map((claim, index) => (
                  <tr key={`${segment.news_item_id}-${index}`}>
                    <td>{claim.text}</td>
                    <td>
                      <blockquote>{claim.source_excerpt}</blockquote>
                      <span className="hint">
                        {claim.source_title} · chars {claim.span.start}–{claim.span.end}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </article>
      ))}
    </section>
  )
}
