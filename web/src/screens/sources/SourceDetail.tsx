// One connected account, in full: what it is, when it last synced and what that sync
// found, what it has pulled in and where that went, and the things you can do to it.
//
// The live counts are derived — `/v1/source-items/held` and `/v1/ingestion`, each by
// `source_id` (`status.ts`, `countsFor`). The rest is read off the source row itself: the
// last sync's result, the filter and the first-sync window (motet#94), and the all-time
// totals.

import { useEffect, useState } from 'react'

import { ApiError, type ProcessingStatus, type Source, api } from '../../api/client'
import { workerState } from '../Processing'
import { DEFAULT_QUERY } from './ConnectGmail'
import { StatusPill } from './IntegrationCard'
import {
  type SourceCounts,
  describeLastSync,
  describeScope,
  isPollable,
  relativeTime,
  rowStatus,
} from './status'
import { LabelSync } from '../LabelSync'

/** How long "Sync now" watches for the poll to land before saying it is still queued. */
const SYNC_WATCH_MS = 120_000
const SYNC_POLL_MS = 2_000

type Sync =
  | { kind: 'idle' }
  /** The poll is enqueued; `before` is the `lastSyncedAt` it has to move past. */
  | { kind: 'queued'; before: string | null; startedAt: number }
  | { kind: 'done'; at: string; queued: number }
  | { kind: 'slow' }
  | { kind: 'error'; message: string }

type Disconnect = { kind: 'idle' } | { kind: 'confirm' } | { kind: 'busy' } | { kind: 'error'; message: string }

type Remove = { kind: 'idle' } | { kind: 'busy' } | { kind: 'error'; message: string }

/**
 * When the last sync ran, for display: `last_sync.at`, or `last_polled_at` for a row polled
 * before the API recorded a result. Not what "Sync now" watches — see `syncedAt` below.
 */
const lastSyncedAt = (source: Source): string | null => source.last_sync?.at ?? source.last_polled_at

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
  const [remove, setRemove] = useState<Remove>({ kind: 'idle' })
  const status = rowStatus(source)
  // What "Sync now" watches: only `last_sync.at`, which a poll writes and nothing else
  // does. `last_polled_at` also moves when extraction skips a message, so watching it
  // would call a skip a sync.
  const syncedAt = source.last_sync?.at ?? null
  const shownSyncAt = lastSyncedAt(source)

  // The poll route enqueues and answers at once; the sync has *run* when the row's last
  // sync time moves. So a queued sync re-fetches the sources list on an interval until it
  // does, and gives up on the watch — not on the sync — after a bound. An interval rather
  // than a timeout re-armed by a change: a re-fetch that comes back unchanged changes
  // nothing, so a watch that waited for a change to schedule the next one stopped after it.
  useEffect(() => {
    if (sync.kind !== 'queued') return
    const timer = window.setInterval(() => {
      if (Date.now() - sync.startedAt > SYNC_WATCH_MS) setSync({ kind: 'slow' })
      else void onRefresh()
    }, SYNC_POLL_MS)
    return () => window.clearInterval(timer)
  }, [sync, onRefresh])

  // A poll that gave up records its error on `last_sync` and moves its time too, so a
  // moved time is "it ran", not "it worked".
  const syncError = source.last_sync?.error ?? null
  const syncQueued = source.last_sync?.queued ?? 0
  useEffect(() => {
    if (sync.kind !== 'queued' || !syncedAt || syncedAt === sync.before) return
    setSync(
      syncError
        ? { kind: 'error', message: `The sync gave up: ${syncError}` }
        : { kind: 'done', at: syncedAt, queued: syncQueued },
    )
  }, [sync, syncedAt, syncError, syncQueued])

  const syncNow = async () => {
    setSync({ kind: 'queued', before: syncedAt, startedAt: Date.now() })
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

  // Removing an abandoned consent row: `DELETE /v1/sources/{id}`, which the API refuses
  // for anything that ever held a credential or pulled an item in.
  const doRemove = async () => {
    setRemove({ kind: 'busy' })
    try {
      await api.removeSource(source.id)
      await onRefresh()
    } catch (err) {
      setRemove({ kind: 'error', message: err instanceof ApiError ? err.message : String(err) })
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
          connected and nothing was changed. Connect again, or remove it: it is not polled.
        </p>
      )}
      {status === 'disconnected' && (
        <p className="notice" role="status">
          Disconnected
          {source.disconnected_at ? ` ${relativeTime(source.disconnected_at, now)}` : ''}. The
          credential is forgotten and the mailbox is no longer polled; everything it pulled in
          stays, because episodes already cite it.
        </p>
      )}
      {source.last_error && (
        <div className="error" role="alert">
          <p>The last sync failed:</p>
          <p className="reason">{source.last_error}</p>
        </div>
      )}

      <dl className="facts source-facts">
        <dt>{status === 'awaiting_consent' ? 'Started' : 'Added'}</dt>
        <dd>
          {new Date(source.created_at).toLocaleDateString()}{' '}
          <span className="hint">({relativeTime(source.created_at, now)})</span>
        </dd>
        {isPollable(source) && (
          <>
            <dt>Last sync</dt>
            <dd>
              {shownSyncAt ? (
                <>
                  {relativeTime(shownSyncAt, now)}{' '}
                  <span className="hint">({new Date(shownSyncAt).toLocaleString()})</span>
                </>
              ) : (
                'Never polled'
              )}
            </dd>
            <dt>Last result</dt>
            <dd className={source.last_sync?.error ? 'error' : undefined}>
              {describeLastSync(source) ?? <span className="hint">No sync has completed yet.</span>}
            </dd>
            {source.query && (
              <>
                <dt>Filter</dt>
                <dd>
                  <code>{source.query}</code>
                  {source.query === DEFAULT_QUERY && <span className="hint"> (the default)</span>}
                </dd>
              </>
            )}
            {source.first_sync_days !== null && (
              <>
                <dt>Sync window</dt>
                <dd>
                  The first sync reached back {source.first_sync_days} day
                  {source.first_sync_days === 1 ? '' : 's'}; older mail was not pulled in.
                </dd>
              </>
            )}
          </>
        )}
        {status !== 'awaiting_consent' && (
          <>
            <dt>All time</dt>
            <dd>
              {source.items_pulled_in} pulled in, {source.items_integrated} ingested
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

      {status === 'awaiting_consent' && (
        <div className="row danger-zone">
          <button
            type="button"
            className="linkish"
            disabled={remove.kind === 'busy'}
            onClick={() => void doRemove()}
          >
            {remove.kind === 'busy' ? 'Removing…' : 'Remove this attempt'}
          </button>
          {remove.kind === 'error' && (
            <span className="error" role="alert">
              {remove.message}
            </span>
          )}
        </div>
      )}

      {/* Label sync (motet#96) — settings, and the one consent that asks for more than
          read-only access. It hands back the updated source; re-fetching everything keeps
          this panel's derived counts in step rather than patching one row. */}
      {isPollable(source) && source.connected && source.label_sync && (
        <LabelSync source={source} onChange={() => void onRefresh()} />
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
          Synced {relativeTime(sync.at, Math.max(now, Date.now()))}.{' '}
          {sync.queued > 0 ? 'New items are held for you to ingest.' : 'Nothing new.'}
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
