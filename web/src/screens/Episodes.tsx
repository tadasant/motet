// The Episodes section: a shelf first, the detail second (motet#89).
//
// The owner's words: "here's all the episodes, some are listened to already some are not,
// click in to see the notes and play control." `EpisodeScreen` is the click-in — notes,
// segments, the player — and it is rendered here under a back link. What this file adds is
// the shelf it sits on: every episode, newest first, sorted into what is still to be heard
// and what has been. Before it, the section *was* the detail, seeded with whichever episode
// App picked, and the only way to another one was an inline "Other episodes:" line.
//
// **This component holds no state that has to outlive it.** It unmounts whenever another
// section is open, so which episode is open lives in App, and so does the list — App's
// refresh reloads it and merges it. The one thing kept here is "play when it opens", which
// is an intent from one click and should *not* survive leaving the section and coming back.
//
// Listened state is derived — see listening.ts for the rule and what it costs.

import { useState } from 'react'

import { ApiError, type Episode, type ProcessingStatus } from '../api/client'
import { EpisodeScreen, IN_PROGRESS } from './EpisodeScreen'
import { formatClock, listenState, markEpisodeListened } from './listening'

/** Listened rows are folded away once there are more than this many. */
export const COLLAPSE_LISTENED_OVER = 5

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

/** When an episode came out or — for one not out yet — when it was asked for. */
function when(episode: Episode): number {
  return Date.parse(episode.published_at ?? episode.created_at)
}

/** Newest first. Exported: App picks the episode a reload lands on by the same order. */
export function newestFirst(a: Episode, b: Episode): number {
  return when(b) - when(a)
}

export function Episodes({
  episodes,
  openId,
  loaded,
  unavailable,
  processing,
  onOpen,
  onBack,
  onPositionReported,
  onChanged,
}: {
  /** Every episode App knows about. */
  episodes: Episode[]
  /** The episode whose detail is showing, or null for the shelf. */
  openId: string | null
  /** Whether the list has been asked for at least once. */
  loaded: boolean
  /** Whether the last attempt to ask failed. */
  unavailable: boolean
  processing: ProcessingStatus | null
  onOpen: (episode: Episode) => void
  onBack: () => void
  onPositionReported: (episodeId: string, listenedThroughMs: number) => void
  /** Something this section wrote changed the backlog and the list: re-ask for both. */
  onChanged: () => void
}) {
  // The Play pill's intent, as opposed to a row click: open *and* start the player. Local,
  // so coming back to the section later does not start playing on its own.
  const [playId, setPlayId] = useState<string | null>(null)
  const [marking, setMarking] = useState<string | null>(null)
  const [error, setError] = useState('')

  const open = episodes.find((entry) => entry.id === openId) ?? null
  if (open) {
    return (
      <>
        <nav className="back-row" aria-label="Episodes navigation">
          <button
            type="button"
            className="linkish"
            onClick={() => {
              setPlayId(null)
              onBack()
            }}
          >
            ← All episodes
          </button>
        </nav>
        {unavailable && <StaleNote />}
        <EpisodeScreen
          episode={open}
          processing={processing}
          autoPlay={playId === open.id}
          onPositionReported={onPositionReported}
          onBacklogChanged={onChanged}
        />
      </>
    )
  }

  const markListened = async (entry: Episode) => {
    setMarking(entry.id)
    try {
      await markEpisodeListened(entry)
      // Moved here at once, rather than on the refresh's answer: the row should leave
      // Up next when it is clicked, not a round trip later.
      onPositionReported(entry.id, entry.duration_ms)
      onChanged()
      setError('')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setMarking(null)
    }
  }

  const all = [...episodes].sort(newestFirst)
  const upNext = all.filter((entry) => listenState(entry) !== 'listened')
  const listened = all.filter((entry) => listenState(entry) === 'listened')

  return (
    // Titled by the shell's top bar, like every other section: a region, not a second
    // heading that says the same word.
    <section aria-label="Episodes" className="episodes">
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {all.length === 0 ? (
        // Three answers, not one: "you have none", "I have not looked yet" and "I could
        // not find out" are different claims, and showing the first for either of the
        // others is the disappearance motet#44 is about.
        <p className="hint">
          {!loaded
            ? 'Looking for your episodes…'
            : unavailable
              ? 'Could not load your episodes. This is not the same as having none.'
              : 'Make one from the backlog.'}
        </p>
      ) : (
        <>
          {unavailable && <StaleNote />}
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
                    onOpen={() => onOpen(entry)}
                    onPlay={() => {
                      setPlayId(entry.id)
                      onOpen(entry)
                    }}
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
                  <EpisodeRow
                    key={entry.id}
                    episode={entry}
                    onOpen={() => onOpen(entry)}
                    onPlay={() => {
                      setPlayId(entry.id)
                      onOpen(entry)
                    }}
                  />
                ))}
              </ul>
            </details>
          )}
        </>
      )}
    </section>
  )
}

/** The list is what was last loaded, and a refresh just failed: say so rather than pose. */
function StaleNote() {
  return (
    <p className="hint" role="status">
      Could not refresh your episodes just now — this is what was last loaded.
    </p>
  )
}

function EpisodeRow({
  episode,
  onOpen,
  onPlay,
  onMarkListened,
  marking = false,
}: {
  episode: Episode
  onOpen: () => void
  onPlay: () => void
  onMarkListened?: () => void
  marking?: boolean
}) {
  const listen = listenState(episode)
  const working = IN_PROGRESS.has(episode.state)
  const failed = episode.state === 'failed'
  const ready = episode.state === 'ready'
  const stories = episode.segments.length
  const play = listen === 'in_progress' ? 'Resume' : 'Play'
  const pct =
    listen === 'in_progress'
      ? Math.min(100, Math.round((episode.listened_through_ms / episode.duration_ms) * 100))
      : 0

  return (
    // The whole row opens the detail for a pointer; the title button is the same action
    // for a keyboard and a screen reader, so the row itself needs no role of its own.
    <li className={`episode-row ${listen} ${episode.state}`} onClick={onOpen} data-listen-state={listen}>
      <div className="episode-main">
        <div className="episode-head">
          <button
            type="button"
            className="linkish episode-title"
            onClick={(event) => {
              // The row would open it too; once is the right number of times.
              event.stopPropagation()
              onOpen()
            }}
          >
            {episode.title}
          </button>
          {working && <span className="badge working">Working…</span>}
          {failed && <span className="badge failed">Failed</span>}
          {ready && listen === 'unlistened' && <span className="badge">Unlistened</span>}
          {ready && listen === 'in_progress' && <span className="badge working">In progress</span>}
          {ready && listen === 'listened' && <span className="badge listened">Listened</span>}
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
          // The accessible name starts with the visible word, so a voice-control user who
          // says "Resume" reaches the button that says it.
          <button type="button" className="play" onClick={onPlay} aria-label={`${play} ${episode.title}`}>
            ▶ {play}
          </button>
        )}
        {ready && listen !== 'listened' && onMarkListened && (
          <button type="button" className="linkish hint" onClick={onMarkListened} disabled={marking}>
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
