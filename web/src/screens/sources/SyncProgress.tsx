// Where a mailbox sync has got to: the step it is on, and — once it has found anything —
// how much has been pulled in against how much is left, with a bar.
//
// Everything here is read off `sync_progress`, which the API assembles from the worker's
// running totals and the job queue. Nothing is inferred from how long the page has been
// watching: a sync that takes twenty minutes is still reported step by step, and one with
// no worker to run it says so instead of spinning.

import { type SyncProgress as Progress, describeSyncProgress } from './status'

export function SyncProgress({ progress }: { progress: Progress }) {
  const shown = describeSyncProgress(progress)
  const percent = shown.fraction === null ? null : Math.round(shown.fraction * 100)
  const alert = shown.tone === 'error' || shown.tone === 'stalled'
  return (
    <div
      className={`sync-progress sync-${shown.tone}`}
      role={alert ? 'alert' : 'status'}
      aria-label="Sync progress"
    >
      <p className="sync-headline">
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
      {shown.count && <p className="sync-count">{shown.count}</p>}
      {shown.detail && <p className="sync-detail">{shown.detail}</p>}
    </div>
  )
}
