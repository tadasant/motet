// Where a mailbox sync has got to: the step it is on, and — once it has found anything —
// how much has been pulled in against how much is left, with a bar.
//
// Everything here is read off `sync_progress`, which the API assembles from the worker's
// running totals and the job queue. Nothing is inferred from how long the page has been
// watching: a sync that takes twenty minutes is still reported step by step, and one with
// no worker to run it says so instead of spinning.
//
// Only the headline and the detail are a live region. The count changes on every poll, and a
// screen reader announcing "Pulled in 121 of 480" every two seconds is noise; the step changing
// — or the sync stalling or giving up — is what is worth saying out loud.

import { type SyncProgress as Progress, describeSyncProgress } from './status'

export function SyncProgress({ progress }: { progress: Progress }) {
  const shown = describeSyncProgress(progress)
  const percent = shown.fraction === null ? null : Math.round(shown.fraction * 100)
  const alert = shown.tone === 'error' || shown.tone === 'stalled'
  return (
    <section className={`sync-progress sync-${shown.tone}`} aria-label="Sync progress">
      <p className="sync-headline" role={alert ? 'alert' : 'status'}>
        {shown.headline}
        {shown.tone === 'working' && <span className="sync-ellipsis" aria-hidden="true">…</span>}
      </p>
      {shown.tone !== 'done' && (
        <div
          className={`sync-bar${percent === null ? ' indeterminate' : ''}`}
          role="progressbar"
          aria-label={shown.count ?? shown.headline}
          aria-valuemin={0}
          aria-valuemax={100}
          {...(percent === null ? {} : { 'aria-valuenow': percent })}
        >
          <span className="sync-bar-fill" style={percent === null ? undefined : { width: `${percent}%` }} />
        </div>
      )}
      {(shown.count || shown.elapsed) && (
        <p className="sync-count">
          {shown.count}
          {/* How long it has been going, so "slow" and "stuck" are tellable apart. Not a
              live region: it changes on every poll and is not worth announcing. */}
          {shown.elapsed && (
            <span className="sync-elapsed">
              {shown.count ? ' · ' : ''}
              {`running for ${shown.elapsed}`}
            </span>
          )}
        </p>
      )}
      {shown.detail && (
        <p className="sync-detail" role="status">
          {shown.detail}
        </p>
      )}
    </section>
  )
}
