// Where an episode is between "make it" and a file to play: the step it is on, a count
// once there is one, how long it has taken against how long one usually takes, and a bar.
//
// **Presentational only.** Every word, the bar's fraction and the tone come from
// `describeBuildProgress`, so what a state *means* stays a unit test with no DOM. This
// file decides only how the five parts are laid out.
//
// It is the Sources screen's `SyncProgress` picture over a different pipeline, and it is
// deliberately a second component rather than a shared one. They would share four of five
// parts — an episode adds the timing line — and merging them would put this change into
// `sources/`, which a concurrent branch owns. Two small components against one shared one
// plus a cross-lane edit; if they ever want a third caller, that is the moment to merge.
//
// Only the headline and the detail are a live region. A count that changes on every poll
// and a screen reader saying "Recorded 4 of 9" every three seconds is noise; the step
// changing — or the build stalling or giving up — is what is worth saying out loud.

import { type BuildDescription } from './episodeProgress'

export function EpisodeProgressPanel({ description }: { description: BuildDescription }) {
  const percent = description.fraction === null ? null : Math.round(description.fraction * 100)
  const alert = description.tone === 'error' || description.tone === 'stalled'
  return (
    <section className={`build-progress build-${description.tone}`} aria-label="Episode progress">
      <p className="build-headline" role={alert ? 'alert' : 'status'}>
        {description.headline}
        {description.tone === 'working' && (
          <span className="build-ellipsis" aria-hidden="true">
            …
          </span>
        )}
      </p>
      {description.tone !== 'done' && (
        <div
          className={`build-bar${percent === null ? ' indeterminate' : ''}`}
          role="progressbar"
          aria-label={description.count ?? description.headline}
          aria-valuemin={0}
          aria-valuemax={100}
          {...(percent === null ? {} : { 'aria-valuenow': percent })}
        >
          <span
            className="build-bar-fill"
            style={percent === null ? undefined : { width: `${percent}%` }}
          />
        </div>
      )}
      {description.count && <p className="build-count">{description.count}</p>}
      {description.timing && <p className="build-timing">{description.timing}</p>}
      {description.detail && (
        <p className="build-detail" role="status">
          {description.detail}
        </p>
      )}
    </section>
  )
}
