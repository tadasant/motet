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

import { ApiError, type Episode, type FeedInfo, type ProcessingStatus, api } from '../api/client'
import { ago, workerState } from './Processing'

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
  episodes,
  processing,
  onEpisodeChanged,
  onSelectEpisode,
  onBacklogChanged,
}: {
  episode: Episode
  episodes: Episode[]
  processing: ProcessingStatus | null
  onEpisodeChanged: (episode: Episode) => void
  onSelectEpisode: (episode: Episode) => void
  onBacklogChanged: () => void
}) {
  const worker = workerState(processing)
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

      {IN_PROGRESS.has(episode.state) &&
        (worker === 'running' || worker === 'unknown' ? (
          <p className="hint" role="status">
            Working… assembly, script, grounding validation, then audio. This page polls.
          </p>
        ) : (
          // The same lie the Processing panel used to tell, one stage later and more
          // expensive: an episode that reached `pending` and has no worker behind it is
          // not working, and "this page polls" invites somebody to sit and watch it.
          <p className="stalled" role="status">
            Not moving: nothing is draining the queues
            {processing?.worker_last_seen_at
              ? ` — a worker last ran ${ago(processing.worker_last_seen_at)}`
              : ' — no worker has ever run here'}
            . Assembly, script and audio all wait on one.
          </p>
        ))}
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

      {/* Every episode there is, so the one you made yesterday is reachable. Before
          motet#44 this screen only ever knew about an episode opened in this page's
          lifetime, which meant a reload lost a finished one for good. */}
      {episodes.length > 1 && (
        <p className="hint">
          Other episodes:{' '}
          {episodes
            .filter((entry) => entry.id !== episode.id)
            .map((entry, index) => (
              <span key={entry.id}>
                {index > 0 && ' · '}
                <button type="button" className="linkish" onClick={() => onSelectEpisode(entry)}>
                  {entry.title}
                </button>{' '}
                <span className="hint">({entry.state})</span>
              </span>
            ))}
        </p>
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
