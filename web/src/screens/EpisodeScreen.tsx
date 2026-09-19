// Screen 3: the episode — a player, and the transcript with each claim beside its source
// span.
//
// This screen is the product's argument that it is not making things up. Every spoken
// sentence is shown next to the verbatim source text it is answerable to (invariant 3),
// which is why the API resolves the span server-side rather than leaving the client to
// fetch sources and hope it bothers.
//
// **There is a player here now, and that reverses a Phase 1 decision** (motet#89). Phase 1
// shipped a private RSS feed *instead* of a player, because a browser cannot do background
// audio or offline and a dog walk needs both. That reason still holds, so the feed URL is
// still offered below and is still the answer for the walk; what changed is that "click in
// to see the notes and play control" is what the owner asked this screen to be. The player
// is for listening at a desk, with the transcript beside it.
//
// It is an `<audio>` element pointed straight at the audio route, driven by the brand's
// transport rather than the browser's own controls (motet#110), and it reports where the
// listener got to through `PUT /v1/episodes/{id}/position`, the write AGENTS.md names for a
// syncing player — so listening here moves the shelf's In progress / Listened state and
// marks stories read as their segments pass, which is invariant 5 reaching this surface.
// The position is ours (invariant 4): it resumes from `listened_through_ms`, never from
// anything the browser remembers.

import { type RefObject, useEffect, useRef, useState } from 'react'

import { ApiError, type Episode, type FeedInfo, type ProcessingStatus, api } from '../api/client'
import { Live } from './Live'
import { formatClock, listenState, markEpisodeListened } from './listening'
import { ago, serverNow, workerState } from './Processing'

/** States a client should keep polling through. Exported: the app polls on it. */
export const IN_PROGRESS = new Set(['pending', 'scripting', 'rendering'])

/**
 * How far playback advances between position reports while the player runs.
 *
 * Pause and the end flush whatever is left, so this bounds only what a closed tab loses.
 * Ten seconds is well inside a segment, which is the granularity read state moves at.
 */
export const REPORT_EVERY_MS = 10_000

/**
 * The largest forward step between two ticks that still counts as listening, at 1×.
 * `timeupdate` fires a few times a second while playing, so anything bigger is a seek.
 */
export const MAX_LISTENING_STEP_MS = 5_000

export function EpisodeScreen({
  episode,
  processing,
  autoPlay = false,
  onPositionReported,
  onBacklogChanged,
}: {
  episode: Episode
  processing: ProcessingStatus | null
  /** Start playing as soon as the audio can — the shelf's Play pill, not a row click. */
  autoPlay?: boolean
  /** The server's answer to a position report: where it now says the listener is. */
  onPositionReported: (episodeId: string, listenedThroughMs: number) => void
  onBacklogChanged: () => void
}) {
  const worker = workerState(processing)
  const [feed, setFeed] = useState<FeedInfo | null>(null)
  const [error, setError] = useState('')
  const [listened, setListened] = useState<number | null>(null)
  const [marking, setMarking] = useState(false)
  const player = useRef<HTMLAudioElement>(null)
  // Where Play Live puts its mic pill: in the player's transport, beside the speed pill. A
  // node rather than a ref so Live re-renders into it when the player (keyed per episode)
  // is replaced.
  const [micSlot, setMicSlot] = useState<HTMLElement | null>(null)

  useEffect(() => {
    api.feed().then(setFeed).catch(() => setFeed(null))
  }, [])

  // No poll of its own. The app reloads the episode list on its refresh while anything is
  // in the pipeline, and this screen renders whichever copy of the episode it is handed —
  // a second poller here would be a second request for the same answer.

  const markListened = async () => {
    setMarking(true)
    try {
      setListened(await markEpisodeListened(episode))
      onPositionReported(episode.id, episode.duration_ms)
      onBacklogChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
    } finally {
      setMarking(false)
    }
  }

  const playable = feed !== null && episode.state === 'ready'
  const seekTo = (ms: number) => {
    const el = player.current
    if (!el) return
    el.currentTime = ms / 1000
    el.play().catch(() => undefined)
  }

  return (
    <section aria-label="Episode">
      <p className="hint episode-heading">
        <strong>{episode.title}</strong>
        <span>
          {episode.state}
          {episode.state === 'ready' && ` · ${formatClock(episode.duration_ms)}`}
        </span>
      </p>

      {IN_PROGRESS.has(episode.state) &&
        (worker === 'running' || worker === 'unknown' || episode.state !== 'pending' ? (
          <p className="hint" role="status">
            Working… assembly, script, then audio. This page polls.
          </p>
        ) : (
          // The same lie the Processing panel used to tell, one stage later and more
          // expensive: an episode that reached `pending` and has no worker behind it is
          // not working, and "this page polls" invites somebody to sit and watch it.
          //
          // Only in `pending`, which is the state nothing has touched yet. Past it a
          // worker demonstrably reached this episode, and a long TTS render is exactly
          // the job that can outlast the heartbeat's freshness window — so the banner
          // would be accusing a worker that is at that moment paying Cartesia.
          <p className="stalled" role="status">
            Not moving: nothing is draining the queues
            {processing?.worker_last_seen_at
              ? ` — a worker last ran ${ago(processing.worker_last_seen_at, serverNow(processing))}`
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

      {playable && (
        // Keyed by episode, so a different episode is a fresh element with a fresh
        // resume point and a fresh report ledger, never the last one's.
        <Player
          key={episode.id}
          episode={episode}
          src={api.audioUrl(episode.id, feed.token)}
          diagnose={() => api.audioProblem(episode.id, feed.token)}
          audioRef={player}
          autoPlay={autoPlay}
          micSlot={setMicSlot}
          onReported={(at, marked) => {
            onPositionReported(episode.id, at)
            if (marked > 0) onBacklogChanged()
          }}
        />
      )}
      {/* Play Live: this player's episode through the voice service, interruptible by
          voice (motet#93). Disabled, with the reason, where no voice service exists. */}
      {playable && <Live episode={episode} player={player} micSlot={micSlot} />}

      {episode.state === 'ready' && (
        <div className="row">
          <button type="button" onClick={markListened} disabled={marking}>
            {marking ? 'Marking…' : 'Mark listened'}
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
          For the walk, listen in a podcast app — background audio and offline are what a
          browser tab cannot do. Paste this private feed URL into Overcast or Apple
          Podcasts: <code className="feed-url">{feed.url}</code>
        </p>
      )}

      {episode.segments.map((segment) => (
        <article key={segment.news_item_id} className="segment">
          <h2>{segment.news_item_title}</h2>
          <p className="hint">
            starts at{' '}
            {playable ? (
              <button
                type="button"
                className="linkish"
                onClick={() => seekTo(segment.start_ms)}
                aria-label={`Play from ${formatClock(segment.start_ms)}: ${segment.news_item_title}`}
              >
                ▶ {formatClock(segment.start_ms)}
              </button>
            ) : (
              formatClock(segment.start_ms)
            )}
          </p>
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

/** The element failed and the route did not refuse: the file is there, this browser balked. */
export const COULD_NOT_PLAY =
  'This episode\u2019s audio is there, but this browser could not load or play it just now. The podcast feed below has the same file.'

/** What the speed pill steps through, in order, from 1×. */
const SPEEDS = [1, 1.2, 1.5, 2, 0.8]

/**
 * The in-page player: resume from the server's position, report the furthest point played.
 *
 * **Pointed at the route, not fetched into a blob.** The prototype fetched the audio into
 * a blob URL because the local storage backend served no `Range` and a browser treats that
 * as unseekable (it serves one now). But a deployed API answers the route with a 307 to a signed URL on the
 * object store's origin, and a `fetch` of that needs CORS on the bucket — which nothing
 * grants — so the player would have silently not appeared anywhere but a laptop. A media
 * element follows the redirect without CORS and gets real range support from the store.
 *
 * **Only continuous listening from what has already been heard moves the position.** The
 * server marks read every story whose segment the position has *passed*, so a reported
 * position is a claim about everything before it. A tick counts only while the element is
 * playing, only as a small forward step (a seek's echo is a jump), and only when it starts
 * at the frontier already heard — so scrubbing, a ▶ jump to a later story, and listening
 * on from past a skip all leave the skipped stories unread. That is the iOS player's rule
 * (`PlaybackController.maxListeningStepMs`) fitted to a route that knows positions rather
 * than coverage. The cost is the safe one: after a skip, Resume lands back at the skip,
 * and Mark listened is the way to say the rest was heard. The end counts as the end only
 * when the frontier had reached its last step, for the same reason.
 */
function Player({
  episode,
  src,
  diagnose,
  audioRef,
  autoPlay,
  micSlot,
  onReported,
}: {
  episode: Episode
  src: string
  /** Asks the route why the element could not load it: the API's sentence, or null. */
  diagnose: () => Promise<string | null>
  audioRef: RefObject<HTMLAudioElement | null>
  autoPlay: boolean
  /** Receives the transport's pill slot, which Play Live renders its mic pill into. */
  micSlot: (node: HTMLElement | null) => void
  onReported: (listenedThroughMs: number, newsItemsMarkedRead: number) => void
}) {
  // Read once, at mount: a report coming back mid-play moves the episode's position, and
  // that must not seek the element under somebody who is listening.
  const [resumeAt] = useState(() =>
    listenState(episode) === 'in_progress' ? episode.listened_through_ms : 0,
  )
  // The frontier heard so far, the furthest the server has been told, and where the
  // element was at the last tick — null after a seek, so the next tick has no step.
  const furthest = useRef(episode.listened_through_ms)
  const sent = useRef(episode.listened_through_ms)
  const last = useRef<number | null>(null)
  // The server's position can move under a mounted player — Mark listened, another device —
  // and it is already heard and already told, so neither ledger should lag it.
  useEffect(() => {
    furthest.current = Math.max(furthest.current, episode.listened_through_ms)
    sent.current = Math.max(sent.current, episode.listened_through_ms)
  }, [episode.listened_through_ms])
  // Refs rather than closure values, because the flush on unmount runs with the props of
  // the render that mounted it.
  const latest = useRef({ id: episode.id, duration: episode.duration_ms, onReported })
  latest.current = { id: episode.id, duration: episode.duration_ms, onReported }

  const flush = () => {
    const { id, duration, onReported: report } = latest.current
    const at = Math.min(furthest.current, duration)
    if (at <= sent.current) return
    sent.current = at
    api
      .setPosition(id, at)
      .then((result) => report(result.listened_through_ms, result.news_items_marked_read))
      // Not rolled back to retry: every report carries the frontier, so the next one
      // covers this one, and a rollback would re-send on every tick for as long as the
      // API was refusing — several requests a second against an expired session.
      .catch(() => undefined)
  }

  // Whatever was played since the last report, when the screen goes away — back to the
  // shelf, another section, another episode.
  useEffect(() => () => flush(), [])

  // What the transport draws. The element is the truth and these follow its events; the
  // position ledgers above are a separate question and are not moved by any of this.
  const [playing, setPlaying] = useState(false)
  const [at, setAt] = useState(resumeAt)
  const [rate, setRate] = useState(1)
  // The browser's own controls used to show a broken player when the audio could not load;
  // the transport has to say so itself, or its play circle is a button that does nothing.
  // What it says is the API's reason where the route refused — audio that retention removed
  // is not a broken browser, and the feed below cannot serve it either — and a sentence
  // about this browser where the route answered and the element still could not play it.
  const [failure, setFailure] = useState<string | null>(null)
  const duration = episode.duration_ms
  const played = duration > 0 ? Math.min(100, (at / duration) * 100) : 0

  const toggle = () => {
    const el = audioRef.current
    if (!el) return
    if (el.paused) el.play()?.catch(() => undefined)
    else el.pause()
  }
  const cycleRate = () => {
    const next = SPEEDS[(SPEEDS.indexOf(rate) + 1) % SPEEDS.length] ?? 1
    setRate(next)
    if (audioRef.current) audioRef.current.playbackRate = next
  }

  return (
    <div className="player">
      {/* The reference transport (brand/GUIDELINES.md): an ink play circle, the track with
          the chord on its played portion, tabular times, a speed pill and the mic pill. The
          element underneath has no controls of its own; these drive it. */}
      <div className="transport" role="group" aria-label="Player">
        <button
          type="button"
          className={`play${playing ? ' playing' : ''}`}
          onClick={toggle}
          aria-label={playing ? 'Pause' : 'Play'}
        />
        <div className="scrub">
          <span className="t">{formatClock(at)}</span>
          <div className="track">
            <div className="played" style={{ width: `${played}%` }} />
            <div className="knob" style={{ left: `${played}%` }} />
            <input
              type="range"
              aria-label="Seek"
              min={0}
              max={duration}
              step={1000}
              value={Math.min(at, duration)}
              aria-valuetext={`${formatClock(at)} of ${formatClock(duration)}`}
              onChange={(event) => {
                const el = audioRef.current
                const ms = Number(event.target.value)
                setAt(ms)
                if (el) el.currentTime = ms / 1000
              }}
            />
          </div>
          <span className="t">{formatClock(duration)}</span>
        </div>
        <span className="pills">
          <button type="button" onClick={cycleRate} aria-label={`Playback speed ${rate}×`}>
            {rate}×
          </button>
          {/* Empty, and React renders nothing else into it: Play Live portals its mic pill here. */}
          <span className="mic-slot" ref={micSlot} />
        </span>
      </div>
      <audio
        ref={audioRef}
        preload="metadata"
        src={src}
        onPlay={() => {
          setPlaying(true)
          setFailure(null)
        }}
        onError={() => {
          setPlaying(false)
          setFailure(COULD_NOT_PLAY)
          void diagnose().then((reason) => {
            if (reason) setFailure(reason)
          })
        }}
        onLoadedMetadata={(event) => {
          const el = event.currentTarget
          if (resumeAt > 0) el.currentTime = resumeAt / 1000
          // A rejected play() is the browser's autoplay policy saying no; the controls are
          // right there, so it is not an error worth showing.
          if (autoPlay) el.play().catch(() => undefined)
        }}
        onSeeking={() => {
          last.current = null
        }}
        onTimeUpdate={(event) => {
          const el = event.currentTarget
          const now = el.currentTime * 1000
          setAt(now)
          const previous = last.current
          last.current = now
          if (el.paused || el.seeking || previous === null) return
          const step = now - previous
          const heard =
            step > 0 &&
            step <= MAX_LISTENING_STEP_MS * Math.max(1, el.playbackRate) &&
            previous <= furthest.current + MAX_LISTENING_STEP_MS
          if (!heard) return
          furthest.current = Math.max(furthest.current, now)
          if (furthest.current - sent.current >= REPORT_EVERY_MS) flush()
        }}
        onPause={() => {
          setPlaying(false)
          flush()
        }}
        onEnded={() => {
          setPlaying(false)
          // Played out from the frontier, the file's end is heard; scrubbed to the end
          // while the frontier sat minutes earlier, it is not.
          const { duration } = latest.current
          if (duration - furthest.current <= MAX_LISTENING_STEP_MS) furthest.current = duration
          flush()
        }}
      />
      {failure && (
        <p className="error" role="alert">
          {failure}
        </p>
      )}
      {resumeAt > 0 && (
        <p className="hint">Resumes at {formatClock(resumeAt)}, where you got to.</p>
      )}
    </div>
  )
}
