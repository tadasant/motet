// One connected account, in full: what it is, when it last synced and what that sync
// found, what it has pulled in and where that went, and the things you can do to it.
//
// The live counts are derived — `/v1/source-items/held` and `/v1/ingestion`, each by
// `source_id` (`status.ts`, `countsFor`). The rest is read off the source row itself: the
// last sync's result, the filter and the first-sync window (motet#94), and the all-time
// totals. A sync in flight is `sync_progress`, which the API assembles from the worker's
// running totals and the job queue; the screen re-fetches while one is in flight
// (`Sources.tsx`), so this panel only renders what it is handed.

import { useState } from 'react'

import { ApiError, type Source, api } from '../../api/client'
import { LabelSync } from '../LabelSync'
import { DEFAULT_QUERY } from './ConnectGmail'
import { DEFAULT_FIRST_SYNC_DAYS, FIRST_SYNC_CHOICES, windowLabel } from './firstSync'
import { StatusPill } from './IntegrationCard'
import { SyncProgress } from './SyncProgress'
import {
  type SourceCounts,
  describeLastSync,
  describeScope,
  isPollable,
  relativeTime,
  rowStatus,
  syncInFlight,
} from './status'

/** "Sync now" itself: the request, not the sync. The sync is `source.sync_progress`. */
type Sync = { kind: 'idle' } | { kind: 'requesting' } | { kind: 'error'; message: string }

type Disconnect = { kind: 'idle' } | { kind: 'confirm' } | { kind: 'busy' } | { kind: 'error'; message: string }

type Remove = { kind: 'idle' } | { kind: 'busy' } | { kind: 'error'; message: string }

/**
 * When the last sync ran, for display: `last_sync.at`, or `last_polled_at` for a row polled
 * before the API recorded a result.
 */
const lastSyncedAt = (source: Source): string | null => source.last_sync?.at ?? source.last_polled_at

export function SourceDetail({
  source,
  counts,
  onRefresh,
  onGoToBacklog,
  now = Date.now(),
}: {
  source: Source
  counts: SourceCounts
  /** Re-fetch everything this panel is derived from. Resolves when the fetch settles. */
  onRefresh: () => Promise<void>
  onGoToBacklog: () => void
  /** Overridden only by tests, so a relative time is deterministic. */
  now?: number
}) {
  const [sync, setSync] = useState<Sync>({ kind: 'idle' })
  const [disconnect, setDisconnect] = useState<Disconnect>({ kind: 'idle' })
  const [remove, setRemove] = useState<Remove>({ kind: 'idle' })
  // The window "Sync further back" would use. Starts on this source's own, so re-opening
  // the panel shows what it is set to rather than resetting to a default.
  const [resyncDays, setResyncDays] = useState(
    source.configured_first_sync_days ?? DEFAULT_FIRST_SYNC_DAYS,
  )
  const status = rowStatus(source)
  const shownSyncAt = lastSyncedAt(source)
  const progress = source.sync_progress ?? null

  // The poll route enqueues and answers at once, with the progress already saying
  // "queued"; re-fetching the list is what hands this panel that answer, and the screen's
  // own poll carries it from there until the sync settles.
  const syncNow = async () => {
    setSync({ kind: 'requesting' })
    try {
      await api.pollSource(source.id)
      await onRefresh()
      setSync({ kind: 'idle' })
    } catch (err) {
      setSync({ kind: 'error', message: err instanceof ApiError ? err.message : String(err) })
    }
  }

  // Widening the window alone would do nothing — the adapter reads it only where a search
  // begins, and this source's search is long past its start — so this is its own route,
  // which sets the window *and* asks the next poll to start a fresh search over it.
  const resync = async () => {
    setSync({ kind: 'requesting' })
    try {
      await api.resyncSource(source.id, resyncDays)
      await onRefresh()
      setSync({ kind: 'idle' })
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

  const syncing = sync.kind === 'requesting' || syncInFlight(progress)

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
                  {source.configured_first_sync_days !== source.first_sync_days && (
                    <span className="hint">
                      {' '}
                      The next one would reach {windowLabel(source.configured_first_sync_days)}.
                    </span>
                  )}
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
          {sync.kind === 'error' && (
            <span className="error" role="alert">
              {sync.message}
            </span>
          )}
        </div>
      )}
      {/* "Sync now" picks up what has arrived since the last poll; this reaches *backwards*,
          which no ordinary poll ever does. Separate controls because they are separate
          questions, and only one of them can pull in mail older than the first sync. */}
      {isPollable(source) && status !== 'awaiting_consent' && source.connected && source.active && (
        <div className="row actions resync">
          <label htmlFor={`resync-${source.id}`}>Sync further back</label>
          <select
            id={`resync-${source.id}`}
            value={resyncDays}
            onChange={(e) => setResyncDays(Number(e.target.value))}
            disabled={syncing}
          >
            {FIRST_SYNC_CHOICES.map((choice) => (
              <option key={choice.days} value={choice.days}>
                {choice.label}
              </option>
            ))}
          </select>
          {/* Keeps its own label while a sync runs rather than becoming a second
              "Syncing…" — two controls saying the same word is a screen that cannot say
              which of them is working. "Sync now" above is the one that reports progress. */}
          <button type="button" onClick={() => void resync()} disabled={syncing}>
            Search again
          </button>
          <p className="hint">
            Searches this mailbox again from the chosen point and keeps that as its window.
            Mail already pulled in is skipped before it is fetched, so nothing is duplicated
            and nothing already ingested is charged for twice.
          </p>
        </div>
      )}
      {isPollable(source) && progress && <SyncProgress progress={progress} />}

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
