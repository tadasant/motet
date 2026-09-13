// PROTOTYPE (proto/local-ux): the Episodes section as a *list first*, detail second.
//
// The owner's words: "here's all the episodes, some are listened to already some are not,
// click in to see the notes and play control." `EpisodeScreen` is the click-in — notes,
// segments, the player, Play Live — and it is rendered here unchanged. What this file adds
// is the shelf it sits on: every episode, newest first, sorted into what is still to be
// heard and what has been.
//
// Listened state is *derived* from two server fields and nothing else: `listened_through_ms`
// against `duration_ms`. There is no per-episode "listened" flag on the API, and adding one
// would be a second definition of a fact invariant 5 says lives on the news item — see
// proto/issues/07-episodes-list.md for the tension that leaves.
//
// Drop-in: `Episodes` takes exactly `EpisodeScreen`'s props, so App.tsx swaps one word.

import { useCallback, useEffect, useState, type ComponentProps } from 'react'

import { ApiError, type Episode, api, apiPostPath } from '../api/client'
import { EpisodeScreen, IN_PROGRESS } from './EpisodeScreen'

type Props = ComponentProps<typeof EpisodeScreen>

/** How far short of the end still counts as having heard the whole thing. */
export const LISTENED_SLACK_MS = 5_000

/** Listened rows are folded away once there are more than this many. */
const COLLAPSE_LISTENED_OVER = 5

/** How often the list re-asks while an episode is still in the pipeline. */
const POLL_INTERVAL_MS = 2_000

export type ListenState = 'unlistened' | 'in_progress' | 'listened'

/**
 * Where the listener is in an episode, from the two fields the server carries.
 *
 * An episode that is not `ready` has no duration yet (0), so it reads as unlistened
 * however far anything says it has been played — there is nothing to have played.
 */
export function listenState(episode: Pick<Episode, 'listened_through_ms' | 'duration_ms'>): ListenState {
  const { listened_through_ms: at, duration_ms: total } = episode
  if (total <= 0 || at <= 0) return 'unlistened'
  if (at >= total - LISTENED_SLACK_MS) return 'listened'
  return 'in_progress'
}

/** `m:ss`, or `h:mm:ss` past an hour. */
export function formatClock(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  const mmss = `${hours > 0 ? String(minutes).padStart(2, '0') : minutes}:${String(seconds).padStart(2, '0')}`
  return hours > 0 ? `${hours}:${mmss}` : mmss
}

function formatDate(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const sameYear = date.getFullYear() === new Date().getFullYear()
  return date.toLocaleDateString(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    ...(sameYear ? {} : { year: 'numeric' }),
  })
}

/** Newest first, by when it was published or — for one not yet out — when it was asked for. */
function newestFirst(a: Episode, b: Episode): number {
  return Date.parse(b.published_at ?? b.created_at) - Date.parse(a.published_at ?? a.created_at)
}

// Which episode this section last showed, across the tab being left and come back to. The
// component unmounts whenever another section is open, so a plain `useState` would forget
// that the list was where it last stood — and, more importantly, would not know that the
// episode it is now being handed is a *new* one, made from the backlog a moment ago, which
// should land on the detail and not on the shelf.
let lastShownId: string | null = null

/** Test seam: forget the last-shown episode so a fresh mount lands on the list. */
export function forgetLastShownEpisode(): void {
  lastShownId = null
}

export function Episodes(props: Props) {
  const { episode, episodes, onEpisodeChanged, onSelectEpisode, onBacklogChanged } = props

  // The list is the landing; the detail is what a click — or a newly made episode — opens.
  //
  // With nothing remembered — a fresh page load whose first visit here is "Make an
  // episode" from the Backlog — a new id and a seeded one look identical from the props,
  // so the tiebreak is the episode's own state: one still in the pipeline is the one
  // somebody just asked for (a create answers `pending`), and its Working… copy and
  // polling live on the detail. A reload mid-render therefore also opens on it, which is
  // where the "not moving" banner is; a finished shelf opens on the list.
  const [showList, setShowList] = useState(() =>
    lastShownId === null ? !IN_PROGRESS.has(episode.state) : lastShownId === episode.id,
  )
  useEffect(() => {
    if (lastShownId !== null && lastShownId !== episode.id) setShowList(false)
    lastShownId = episode.id
  }, [episode.id])

  // The list's own copy of the episodes, fetched on mount and while anything is still
  // rendering. App.tsx loads the list once per page load, so without this a "Mark
  // listened" here — or a render finishing in the background — would not show until a
  // reload. Merged over the props rather than replacing them: an episode created a moment
  // ago is in the props before the server's list has it.
  const [fresh, setFresh] = useState<Map<string, Episode>>(new Map())
  const [error, setError] = useState('')
  const reload = useCallback(() => {
    api
      .episodes()
      .then((list) => {
        setFresh(new Map(list.map((entry) => [entry.id, entry])))
        setError('')
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : String(err)))
  }, [])
  useEffect(() => {
    if (showList) reload()
  }, [showList, reload])

  const merged = new Map<string, Episode>()
  for (const entry of episodes) merged.set(entry.id, entry)
  for (const [id, entry] of fresh) merged.set(id, entry)
  if (!merged.has(episode.id)) merged.set(episode.id, episode)
  const all = [...merged.values()].sort(newestFirst)

  // Keep App's notion of the selected episode in step with the server's, so that opening
  // the detail after a render finished in the list does not show a stale "rendering".
  // Same id, so this never flips the list to the detail.
  const selectedFresh = fresh.get(episode.id)
  useEffect(() => {
    if (!selectedFresh) return
    if (
      selectedFresh.state !== episode.state ||
      selectedFresh.listened_through_ms !== episode.listened_through_ms
    ) {
      onEpisodeChanged(selectedFresh)
    }
  }, [selectedFresh, episode.state, episode.listened_through_ms, onEpisodeChanged])

  // Poll while anything is still in the pipeline, list view only: the detail polls on its
  // own (EpisodeScreen) and two pollers would be two requests for one answer.
  const anyWorking = all.some((entry) => IN_PROGRESS.has(entry.state))
  useEffect(() => {
    if (!showList || !anyWorking) return
    const timer = setInterval(reload, POLL_INTERVAL_MS)
    return () => clearInterval(timer)
  }, [showList, anyWorking, reload])

  const open = (entry: Episode) => {
    onSelectEpisode(entry)
    setShowList(false)
  }

  // "Mark listened" writes both facts the server has: every news item read (invariant 5,
  // the same call the detail's button makes) *and* the position at the end, so the row
  // moves to Listened for the same reason a walk to the end would move it. There is no
  // "Mark unlistened": the position is monotonic on the server by design, so there is
  // nothing to write. See proto/issues/07-episodes-list.md.
  const [marking, setMarking] = useState<string | null>(null)
  const markListened = async (entry: Episode) => {
    setMarking(entry.id)
    try {
      await api.markListened(entry.id)
      await apiPostPath(
        '/v1/episodes/{episode_id}/progress',
        `/v1/episodes/${encodeURIComponent(entry.id)}/progress`,
        { listened_through_ms: entry.duration_ms },
      )
      onBacklogChanged()
      reload()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setMarking(null)
    }
  }

  if (!showList) {
    return (
      <>
        <nav className="back-row" aria-label="Episodes navigation">
          <button type="button" className="linkish" onClick={() => setShowList(true)}>
            ← All episodes
          </button>
        </nav>
        <EpisodeScreen {...props} />
      </>
    )
  }

  const upNext = all.filter((entry) => listenState(entry) !== 'listened')
  const listened = all.filter((entry) => listenState(entry) === 'listened')

  return (
    <section aria-labelledby="episodes-heading" className="episodes">
      <h2 id="episodes-heading">Episodes</h2>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {all.length === 0 ? (
        <p className="hint">No episodes yet — make one from the Backlog.</p>
      ) : (
        <>
          <section aria-labelledby="up-next-heading" className="episode-group">
            <h3 id="up-next-heading">
              Up next <span className="hint">({upNext.length})</span>
            </h3>
            {upNext.length === 0 ? (
              <p className="hint">All caught up — everything here has been heard.</p>
            ) : (
              <ul className="episode-list">
                {upNext.map((entry) => (
                  <EpisodeRow
                    key={entry.id}
                    episode={entry}
                    onOpen={() => open(entry)}
                    onMarkListened={() => markListened(entry)}
                    marking={marking === entry.id}
                  />
                ))}
              </ul>
            )}
          </section>

          {listened.length > 0 && (
            <details className="episode-group" open={listened.length <= COLLAPSE_LISTENED_OVER}>
              <summary>
                <h3>
                  Listened <span className="hint">({listened.length})</span>
                </h3>
              </summary>
              <ul className="episode-list">
                {listened.map((entry) => (
                  <EpisodeRow key={entry.id} episode={entry} onOpen={() => open(entry)} />
                ))}
              </ul>
            </details>
          )}
        </>
      )}
    </section>
  )
}

function EpisodeRow({
  episode,
  onOpen,
  onMarkListened,
  marking = false,
}: {
  episode: Episode
  onOpen: () => void
  onMarkListened?: () => void
  marking?: boolean
}) {
  const listen = listenState(episode)
  const working = IN_PROGRESS.has(episode.state)
  const failed = episode.state === 'failed'
  const ready = episode.state === 'ready'
  const stories = episode.segments.length
  const pct =
    listen === 'in_progress' && episode.duration_ms > 0
      ? Math.min(100, Math.round((episode.listened_through_ms / episode.duration_ms) * 100))
      : 0

  return (
    <li
      className={`episode-row ${listen} ${episode.state}`}
      onClick={onOpen}
      data-listen-state={listen}
    >
      <div className="episode-main">
        <div className="episode-head">
          <button type="button" className="linkish episode-title" onClick={onOpen}>
            {episode.title}
          </button>
          {working && <span className="badge running">Working…</span>}
          {failed && <span className="badge failed">Failed</span>}
          {ready && listen === 'unlistened' && <span className="badge">Unlistened</span>}
          {ready && listen === 'in_progress' && <span className="badge running">In progress</span>}
          {ready && listen === 'listened' && <span className="badge done">Listened</span>}
        </div>
        <p className="hint episode-meta">
          {formatDate(episode.published_at ?? episode.created_at)}
          {ready && ` · ${formatClock(episode.duration_ms)}`}
          {` · ${stories} ${stories === 1 ? 'story' : 'stories'}`}
          {working && ` · ${episode.state}`}
        </p>
        {listen === 'in_progress' && (
          <div className="episode-progress">
            <div
              className="progress"
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={episode.duration_ms}
              aria-valuenow={episode.listened_through_ms}
              aria-label={`${formatClock(episode.listened_through_ms)} of ${formatClock(episode.duration_ms)}`}
            >
              <div className="progress-fill" style={{ width: `${pct}%` }} />
            </div>
            <span className="hint progress-text">
              {formatClock(episode.listened_through_ms)} of {formatClock(episode.duration_ms)}
            </span>
          </div>
        )}
        {failed && episode.last_error && <p className="hint episode-error">{episode.last_error}</p>}
      </div>
      <div className="episode-actions" onClick={(event) => event.stopPropagation()}>
        {ready && (
          <button type="button" className="play" onClick={onOpen} aria-label={`Play ${episode.title}`}>
            ▶ {listen === 'in_progress' ? 'Resume' : 'Play'}
          </button>
        )}
        {ready && listen !== 'listened' && onMarkListened && (
          <button
            type="button"
            className="linkish hint"
            onClick={onMarkListened}
            disabled={marking}
          >
            {marking ? 'Marking…' : 'Mark listened'}
          </button>
        )}
        {!ready && (
          <button type="button" className="linkish hint" onClick={onOpen}>
            Details
          </button>
        )}
      </div>
    </li>
  )
}
