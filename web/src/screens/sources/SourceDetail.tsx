// One connected account, in full: what it is, when it last synced, what it has pulled in
// and where that went, and the two things you can do to it.
//
// The numbers here are derived rather than reported — `/v1/source-items/held` filtered
// by `source_id`, `/v1/ingestion` filtered by `source_kind` — and the panel says so where
// the derivation is lossy (`status.ts`, `countsFor`). What the API does not carry at all
// is said out loud rather than left blank: a last-sync *result*, the sync window, the
// filter. Those are the API gaps proto/issues/08 lists, and a panel that quietly omitted
// them would hide the fact that they are missing.

import { useEffect, useState } from 'react'

import { ApiError, type ProcessingStatus, type Source, api } from '../../api/client'
import { workerState } from '../Processing'
import { StatusPill } from './IntegrationCard'
import {
  type SourceCounts,
  describeScope,
  isPollable,
  relativeTime,
  rowStatus,
} from './status'

/** How long "Sync now" watches for the poll to land before saying it is still queued. */
const SYNC_WATCH_MS = 120_000
const SYNC_POLL_MS = 2_000

type Sync =
  | { kind: 'idle' }
  /** The poll is enqueued; `before` is the `last_polled_at` it has to move past. */
  | { kind: 'queued'; before: string | null; startedAt: number }
  | { kind: 'done'; at: string }
  | { kind: 'slow' }
  | { kind: 'error'; message: string }

type Disconnect = { kind: 'idle' } | { kind: 'confirm' } | { kind: 'busy' } | { kind: 'error'; message: string }

export function SourceDetail({
  source,
  counts,
  processing,
  onRefresh,
  onGoToBacklog,
  now = Date.now(),
}: {
  source: Source
  counts: SourceCounts
  /** The worker heartbeat, so "queued" can say whether anything will pick it up. */
  processing: ProcessingStatus | null
  /** Re-fetch everything this panel is derived from. Resolves when the fetch settles. */
  onRefresh: () => Promise<void>
  onGoToBacklog: () => void
  /** Overridden only by tests, so a relative time is deterministic. */
  now?: number
}) {
  const [sync, setSync] = useState<Sync>({ kind: 'idle' })
  const [disconnect, setDisconnect] = useState<Disconnect>({ kind: 'idle' })
  const status = rowStatus(source)

  // The poll route enqueues and answers at once; the sync has *run* when the row's
  // `last_polled_at` moves. So a queued sync watches the sources list until it does,
  // and gives up on the watch — not on the sync — after a bound.
  useEffect(() => {
    if (sync.kind !== 'queued') return
    if (source.last_polled_at !== sync.before && source.last_polled_at) {
      setSync({ kind: 'done', at: source.last_polled_at })
      return
    }
    if (Date.now() - sync.startedAt > SYNC_WATCH_MS) {
      setSync({ kind: 'slow' })
      return
    }
    const timer = window.setTimeout(() => void onRefresh(), SYNC_POLL_MS)
    return () => window.clearTimeout(timer)
  }, [sync, source.last_polled_at, onRefresh])

  const syncNow = async () => {
    setSync({ kind: 'queued', before: source.last_polled_at, startedAt: Date.now() })
    try {
      await api.pollSource(source.id)
    } catch (err) {
      setSync({ kind: 'error', message: err instanceof ApiError ? err.message : String(err) })
    }
  }

  const doDisconnect = async () => {
    setDisconnect({ kind: 'busy' })
    try {
      await api.disconnectSource(source.id)
      await onRefresh()
      setDisconnect({ kind: 'idle' })
    } catch (err) {
      setDisconnect({ kind: 'error', message: err instanceof ApiError ? err.message : String(err) })
    }
  }

  const worker = workerState(processing)
  const syncing = sync.kind === 'queued'

  return (
    <div className={`source-detail row-status-${status}`} aria-label={source.name}>
      <div className="source-detail-head">
        <strong className="source-account">{source.name}</strong>
        <StatusPill status={status === 'ready' ? 'always_on' : status} />
      </div>

      {status === 'awaiting_consent' && (
        <p className="notice" role="status">
          This row was created when Connect was pressed and no credential ever arrived — you
          cancelled on Google&rsquo;s page, or closed it before finishing. Nothing was
          connected and nothing was changed. Connect again below, or leave it: it is not
          polled.
        </p>
      )}
      {status === 'disconnected' && (
        <p className="notice" role="status">
          Disconnected. The credential is forgotten and the mailbox is no longer polled;
          everything it pulled in stays, because episodes already cite it.
        </p>
      )}
      {source.last_error && (
        <div className="error" role="alert">
          <p>The last sync failed:</p>
          <p className="reason">{source.last_error}</p>
        </div>
      )}

      <dl className="facts source-facts">
        <dt>{status === 'awaiting_consent' ? 'Started' : 'Connected'}</dt>
        <dd>
          {new Date(source.created_at).toLocaleDateString()}{' '}
          <span className="hint">({relativeTime(source.created_at, now)})</span>
        </dd>
        {isPollable(source) && (
          <>
            <dt>Last sync</dt>
            <dd>
              {source.last_polled_at ? (
                <>
                  {relativeTime(source.last_polled_at, now)}{' '}
                  <span className="hint">({new Date(source.last_polled_at).toLocaleString()})</span>
                </>
              ) : (
                'Never polled'
              )}
            </dd>
            <dt>Last result</dt>
            <dd className="hint">
              Not reported — the API records when the last poll ran, not what it found.
            </dd>
            <dt>Filter</dt>
            <dd className="hint">Not exposed by the API; the search you entered is used as given.</dd>
            <dt>Sync window</dt>
            <dd className="hint">
              Not exposed. A first sync reads the last 7 days by default (issue 02).
            </dd>
          </>
        )}
        {source.scopes.length > 0 && (
          <>
            <dt>Access</dt>
            <dd>{source.scopes.map(describeScope).join(', ')}</dd>
          </>
        )}
      </dl>

      {/* A row that never connected has pulled nothing in and cannot sync: the tiles and
          the button would be four zeros and a disabled control under a notice that
          already says why. */}
      {status !== 'awaiting_consent' && (
        <div className="stat-row" aria-label="What this source has pulled in">
          <Stat label="Waiting for you" value={counts.held} emphasis={counts.held > 0} />
          <Stat label="Processing" value={counts.processing} />
          <Stat label="Failed" value={counts.failed} bad={counts.failed > 0} />
          <Stat label="Landed recently" value={counts.integrated} />
        </div>
      )}
      {counts.byKind && (
        <p className="hint stat-note">
          Processing, failed and landed are counted by source kind: the API does not say which
          mailbox a processing item came from, so with two mailboxes those three are shared.
        </p>
      )}
      {counts.held > 0 && (
        <p className="waiting-hint">
          <strong>
            {counts.held} item{counts.held === 1 ? '' : 's'} waiting
          </strong>{' '}
          — pulled in and not yet processed.{' '}
          <button type="button" className="linkish" onClick={onGoToBacklog}>
            Review them in Backlog
          </button>
        </p>
      )}

      {isPollable(source) && status !== 'awaiting_consent' && (
        <div className="row actions">
          <button
            type="button"
            className="primary"
            onClick={() => void syncNow()}
            disabled={syncing || !source.connected || !source.active}
            title={
              !source.connected
                ? 'No credential to sync with.'
                : !source.active
                  ? 'Paused: not polled.'
                  : undefined
            }
          >
            {syncing ? 'Syncing…' : 'Sync now'}
          </button>
          <SyncStatus sync={sync} worker={worker} now={now} />
        </div>
      )}

      {isPollable(source) && source.connected && (
        <div className="row danger-zone">
          {disconnect.kind === 'confirm' ? (
            <>
              <span>Disconnect this mailbox? It stops being polled; what it pulled in stays.</span>
              <button type="button" className="danger" onClick={() => void doDisconnect()}>
                Yes, disconnect
              </button>
              <button type="button" onClick={() => setDisconnect({ kind: 'idle' })}>
                Cancel
              </button>
            </>
          ) : (
            <button
              type="button"
              className="linkish"
              disabled={disconnect.kind === 'busy'}
              onClick={() => setDisconnect({ kind: 'confirm' })}
            >
              {disconnect.kind === 'busy' ? 'Disconnecting…' : 'Disconnect'}
            </button>
          )}
          {disconnect.kind === 'error' && (
            <span className="error" role="alert">
              {disconnect.message}
            </span>
          )}
        </div>
      )}
    </div>
  )
}

function Stat({
  label,
  value,
  emphasis = false,
  bad = false,
}: {
  label: string
  value: number
  emphasis?: boolean
  bad?: boolean
}) {
  return (
    <div className={`stat${emphasis ? ' emphasis' : ''}${bad ? ' bad' : ''}${value === 0 ? ' zero' : ''}`}>
      <span className="stat-value">{value}</span>
      <span className="stat-label">{label}</span>
    </div>
  )
}

/**
 * What "Sync now" is doing, in words that do not promise more than the queue does.
 *
 * "Queued" and "a worker has it" are different sentences, and the heartbeat is what tells
 * them apart (motet#38): a queued poll with no worker alive is a poll nothing will run,
 * and saying "syncing…" over it would be the never-infer-"no errors"-from-"no data" trap.
 */
function SyncStatus({ sync, worker, now }: { sync: Sync; worker: ReturnType<typeof workerState>; now: number }) {
  switch (sync.kind) {
    case 'idle':
      return null
    case 'queued':
      return (
        <span className="hint" role="status">
          {worker === 'running'
            ? 'Queued — a worker is running and will pick it up.'
            : worker === 'unknown'
              ? 'Queued. Whether a worker is running could not be checked.'
              : 'Queued — but no worker has run in the last five minutes, so nothing will pick it up until one does.'}
        </span>
      )
    case 'done':
      return (
        <span className="ok" role="status">
          Synced {relativeTime(sync.at, Math.max(now, Date.now()))}. New items are held for you to ingest.
        </span>
      )
    case 'slow':
      return (
        <span className="hint" role="status">
          Still queued after two minutes. It runs when a worker gets to it; this panel stops
          watching, and Last sync updates when you come back.
        </span>
      )
    case 'error':
      return (
        <span className="error" role="alert">
          {sync.message}
        </span>
      )
  }
}
