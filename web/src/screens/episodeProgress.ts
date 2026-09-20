// The pure half of "where is my episode": `build_progress` turned into the four things a
// person waiting for one needs — which step, how far through it, how long it has taken,
// and what to do if it stopped.
//
// Kept apart from the components so that every state is a test with no DOM, and so that
// the phone can be a rule-for-rule port of this file (`MotetKit/Model/EpisodeProgress.swift`)
// rather than a second opinion. Where this changes, change that with it.
//
// Nothing here infers anything from how long the page has been watching. An episode that
// takes twenty minutes is reported step by step, and one nothing will build says so.

import type { Episode } from '../api/client'

export type BuildProgress = NonNullable<Episode['build_progress']>

/**
 * What a build is doing, as the five things a person waiting for one needs.
 *
 * - `headline` — the step. Never a bare "Working…".
 * - `count` — the work done against the work there is, once there is a number for it.
 * - `timing` — how long it has taken, and how long one usually takes.
 * - `detail` — why it is retrying, why it stopped, or what happens next.
 * - `fraction` — drives a determinate bar; `null` is an indeterminate one, for the steps
 *   before anything can be counted. A bar with no denominator would be a made-up number.
 * - `tone` — `stalled` is the case where a moving bar would be the
 *   never-infer-"no errors"-from-"no data" trap (motet#38).
 */
export type BuildDescription = {
  headline: string
  count: string | null
  timing: string | null
  detail: string | null
  fraction: number | null
  tone: 'working' | 'stalled' | 'done' | 'error'
}

/** Stages in which something is still happening, so the screen keeps asking. */
const IN_FLIGHT = new Set(['queued', 'running', 'retrying'])

/** Whether the build is still going: the screen polls and the row reads "Working…". */
export const buildInFlight = (progress: BuildProgress | null | undefined): boolean =>
  !!progress && IN_FLIGHT.has(progress.stage)

/**
 * Whether it is worth re-reading every few seconds: in flight, and something is on it.
 *
 * A build no worker will run moves when a worker appears, which is not a three-second
 * question — so the tab drops back to a slow interval rather than polling a stall forever.
 * The phone's `EpisodeProgress.moving` is the same rule; both are motet#136's, one
 * pipeline along.
 */
export const buildMoving = (progress: BuildProgress | null | undefined): boolean =>
  buildInFlight(progress) && !progress?.waiting_on_worker

/** What each step is called where a sentence needs its name rather than its verb. */
export const STEP_NAMES: Record<string, string> = {
  assemble: 'choosing the stories',
  script: 'writing the script',
  tts: 'recording the audio',
}

const NO_WORKER =
  'No worker has run in the last five minutes, so this will not move until one does.'
// A worker has been asked for and its container has not appeared yet. Where the API starts
// the worker itself — production — that is every build's first minute or two, and saying
// "no worker will run this" over it is the mistake motet#136 shipped on the sync panel.
// The sync panel's own sentence (motet#137), word for word: the two panels sit on one
// screen and must not disagree about what a starting worker is called. It stops at the ask
// deliberately — `MOTET_DRAIN_TRIGGER` says an ask was made, never that Cloud Run obliged.
const STARTING = 'A worker has been asked for — that usually takes a minute or two.'

/**
 * `1,480`. The iOS port has the same separator by hand (`EpisodeProgress.number`, which
 * borrows `SourceStatus`'), because a `FormatStyle` there and a locale here would be two
 * different answers. Used on every number both files print, including the row form.
 */
const n = (value: number): string => value.toLocaleString('en-US')

/**
 * `1m 33s`, `12s`, `1h 04m`. Short enough to sit inside a sentence, and never `0s` for
 * something that has only just started — `just started` is what that actually means.
 */
export function formatElapsed(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000))
  if (seconds < 1) return 'just started'
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ${String(seconds % 60).padStart(2, '0')}s`
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m`
}

/**
 * How long it has taken and — only where the server had a basis for one — how long it
 * usually takes.
 *
 * **Labelled as an estimate every time, and never as a countdown.** The server sends the
 * median of the last few finished episodes and how many that was; the honest way to show
 * that is "usually about 1m 30s", not "about 18s remaining", because subtracting one from
 * the other produces a number that goes negative and then sits there. With fewer than
 * three finished episodes there is no estimate and this is the elapsed time alone, which
 * is the answer when there is no sound basis for anything more.
 */
export function describeTiming(progress: BuildProgress): string | null {
  if (!buildInFlight(progress)) return null
  const elapsed = `${formatElapsed(progress.elapsed_ms)} so far`
  if (progress.estimate_ms === null || progress.estimate_ms === undefined) return elapsed
  const of = progress.estimate_samples === 1 ? 'episode' : 'episodes'
  return (
    `${elapsed} · usually about ${formatElapsed(progress.estimate_ms)} ` +
    `(an estimate, from the last ${progress.estimate_samples} ${of})`
  )
}

/** "Step 2 of 3 · writing the script", the coarse position that is always available. */
function stepLine(progress: BuildProgress): string | null {
  if (!buildInFlight(progress) || !progress.step) return null
  const name = STEP_NAMES[progress.step]
  const where = `Step ${progress.steps_done + 1} of ${progress.steps_total}`
  return name ? `${where} · ${name}` : where
}

/**
 * An episode being built, in words and a bar — `EpisodeBuildProgress` on the API, which
 * joins the episode row to the three queues it travels through.
 *
 * The one bar with a real denominator is the render's: TTS is a loop over segments and the
 * worker reports each one (migration 0025). Assembly and scripting are a single model call
 * each, so there is nothing inside them to count and the bar is indeterminate rather than
 * a fraction somebody invented.
 */
export function describeBuildProgress(progress: BuildProgress): BuildDescription {
  const stalled = progress.waiting_on_worker
  // `worker_starting` is optional on the wire (an older API omits it): absent is false.
  const starting = !!progress.worker_starting
  const total = progress.news_items
  const stories = `${n(total)} ${total === 1 ? 'story' : 'stories'}`
  const rendering = progress.step === 'tts' && total > 0
  const count = rendering
    ? `Recorded ${n(progress.segments_rendered)} of ${n(total)} segments · ` +
      `${n(Math.max(0, total - progress.segments_rendered))} left`
    : progress.step === 'script' && total > 0
      ? stories
      : null
  const fraction = rendering ? Math.min(1, progress.segments_rendered / total) : null
  const timing = describeTiming(progress)

  const working = (headline: string, detail: string | null): BuildDescription => ({
    headline,
    count,
    timing,
    detail: stalled ? NO_WORKER : starting ? STARTING : detail,
    fraction,
    tone: stalled ? 'stalled' : 'working',
  })

  if (progress.stage === 'ready') {
    return {
      headline: `Ready to play · made in ${formatElapsed(progress.elapsed_ms)}`,
      count: null,
      timing: null,
      detail: null,
      fraction: 1,
      tone: 'done',
    }
  }

  if (progress.stage === 'failed') {
    const step = progress.step ? STEP_NAMES[progress.step] : null
    return {
      headline: step ? `Stopped while ${step}` : 'This episode could not be made',
      count: null,
      timing: null,
      detail:
        (progress.error ? `${progress.error} ` : '') +
        'Nothing will move it on its own — make the episode again.',
      // How far it got, as a *determinate* bar. Null here would draw the indeterminate
      // one, whose whole job is to sweep — a moving bar over a build that has stopped is
      // the never-infer-"no errors"-from-"no data" trap pointing the other way.
      fraction: progress.steps_done / progress.steps_total,
      tone: 'error',
    }
  }

  if (progress.stage === 'retrying') {
    const step = progress.step ? STEP_NAMES[progress.step] : 'a step'
    const attempt =
      progress.attempt > 0 ? ` (attempt ${progress.attempt} of ${progress.max_attempts})` : ''
    return working(
      `Trying again after a failure${attempt}`,
      progress.error ? `Stopped while ${step}. Last attempt: ${progress.error}` : null,
    )
  }

  if (progress.stage === 'queued') {
    switch (progress.step) {
      case 'assemble':
        return working(
          starting ? 'Starting a worker to build the episode' : 'Waiting for a worker to start',
          stepLine(progress),
        )
      case 'script':
        return working('Queued to write the script', stepLine(progress))
      case 'tts':
        return working('Queued to record the audio', stepLine(progress))
      default:
        return working('Queued', stepLine(progress))
    }
  }

  if (progress.stage === 'running') {
    switch (progress.step) {
      case 'assemble':
        return working(
          'Choosing which stories fit',
          'Everything unread, oldest first, up to the length you asked for.',
        )
      case 'script':
        return working(
          total > 0 ? `Writing the script for ${stories}` : 'Writing the script',
          'One pass over every story, with the source span behind each claim.',
        )
      case 'tts':
        return working('Recording the audio', stepLine(progress))
      default:
        return working('Working', stepLine(progress))
    }
  }

  // A stage this build does not know — a newer API. Said plainly rather than guessed at.
  return working('Working', stepLine(progress))
}

/**
 * The same reading squeezed into one clause, for a shelf row.
 *
 * A row is a glance and the detail is one click in, so what a row gets is the step and
 * the clock — "recording the audio, 4 of 9 · 1m 12s so far" — rather than the bar, the
 * estimate and the remedy. It is the same `describeBuildProgress` underneath, so the two
 * surfaces cannot disagree about which step an episode is on.
 */
export function describeRowProgress(progress: BuildProgress): string {
  const elapsed = formatElapsed(progress.elapsed_ms)
  if (progress.stage === 'ready') return `made in ${elapsed}`
  if (progress.stage === 'failed') return 'stopped'
  const step = progress.step ? STEP_NAMES[progress.step] : null
  const where =
    progress.stage === 'queued'
      ? step
        ? `queued · ${step}`
        : 'queued'
      : progress.stage === 'retrying'
        ? `trying again · ${step ?? 'a step'}`
        : (step ?? 'working')
  const counted =
    progress.step === 'tts' && progress.news_items > 0
      ? `${where}, ${n(progress.segments_rendered)} of ${n(progress.news_items)}`
      : where
  return `${counted} · ${elapsed}`
}
