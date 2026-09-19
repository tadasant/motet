// The pure half of the Sources screen: what a row means, and what a source has pulled in.
//
// Kept apart from the components so that "is this row connected, paused, abandoned or
// disconnected" is a function a test can call with a fixture, and so that the same
// answer is given by the card pill, the account row and the detail panel — one reading
// per row rather than three.

import type { HeldSourceItem, IngestionItem, Source } from '../../api/client'
import type { Integration } from './catalog'

/**
 * `paste` is the one kind with nothing behind it: no credential to grant and nothing to
 * fetch on a schedule. Every other kind is a mailbox or a feed reached with a grant.
 */
export const PASTE_KIND = 'paste'

/** Whether `connected` says anything about this source. Pasted text has no credential. */
export const needsConsent = (source: Source): boolean => source.kind !== PASTE_KIND

/** Whether anything ever polls it. Pasted text arrives when you paste it, and not before. */
export const isPollable = (source: Source): boolean => source.kind !== PASTE_KIND

/**
 * What one source row is doing, read against the kind that decides which fields apply.
 *
 * - `connected` — a credential is stored and the source is polled.
 * - `paused` — connected, not polled, nothing lost (`active: false`).
 * - `error` — connected, and the last poll failed; `last_error` says how.
 * - `awaiting_consent` — the row `POST /v1/sources/connect` creates *before* the user
 *   leaves for Google, still without a credential. It appears the instant Connect is
 *   pressed and stays if the user cancels on Google's page or closes the tab, so it is
 *   an abandoned attempt rather than a broken source, and must not read like one.
 * - `disconnected` — no credential, because it was forgotten through
 *   `DELETE /v1/sources/{id}/credentials`. `disconnected_at` says so (motet#90). A row
 *   disconnected before the API recorded that has it null, and for those having polled
 *   is the tell: a row that never polled and was never disconnected was never connected.
 * - `ready` / `paused` for the paste source, off `active` alone: it is created active
 *   and never connects, because there is nothing to connect (motet#39).
 */
export type RowStatus =
  | 'connected'
  | 'paused'
  | 'error'
  | 'awaiting_consent'
  | 'disconnected'
  | 'ready'

export function rowStatus(source: Source): RowStatus {
  if (!needsConsent(source)) return source.active ? 'ready' : 'paused'
  if (!source.connected) {
    return source.disconnected_at || source.last_polled_at ? 'disconnected' : 'awaiting_consent'
  }
  if (source.last_error) return 'error'
  return source.active ? 'connected' : 'paused'
}

/**
 * What the *card* says, folding every row of an integration into one pill.
 *
 * Precedence is "the thing that needs attention first": an error beats a healthy
 * connection, a healthy connection beats a paused one, and an abandoned attempt only
 * shows when it is all there is — a person with one working mailbox and one cancelled
 * consent has a connected Gmail, not a pending one.
 */
export type CardStatus =
  | 'connected'
  | 'error'
  | 'paused'
  | 'awaiting_consent'
  | 'disconnected'
  | 'not_connected'
  | 'always_on'
  | 'coming_soon'

export function cardStatus(integration: Integration, rows: Source[]): CardStatus {
  if (integration.availability === 'coming_soon') return 'coming_soon'
  if (integration.availability === 'builtin') return 'always_on'
  const statuses = rows.map(rowStatus)
  if (statuses.includes('error')) return 'error'
  if (statuses.includes('connected')) return 'connected'
  if (statuses.includes('paused')) return 'paused'
  if (statuses.includes('awaiting_consent')) return 'awaiting_consent'
  if (statuses.includes('disconnected')) return 'disconnected'
  return 'not_connected'
}

export function cardStatusLabel(status: CardStatus, count: number): string {
  switch (status) {
    case 'connected':
      return count > 1 ? `${count} connected` : 'Connected'
    case 'error':
      return 'Error'
    case 'paused':
      return 'Paused'
    case 'awaiting_consent':
      return 'Awaiting consent'
    case 'disconnected':
      return 'Disconnected'
    case 'not_connected':
      return 'Not connected'
    case 'always_on':
      return 'Always on'
    case 'coming_soon':
      return 'Coming soon'
  }
}

/** The primary action a card offers, from its status. `null` means a disabled button. */
export type CardAction = 'connect' | 'manage' | 'open_paste' | null

export function cardAction(status: CardStatus): CardAction {
  switch (status) {
    case 'coming_soon':
      return null
    case 'always_on':
      return 'open_paste'
    case 'not_connected':
    case 'awaiting_consent':
      return 'connect'
    default:
      return 'manage'
  }
}

/**
 * What one source has pulled in, and where it is right now.
 *
 * - `held` — extracted, waiting for "ingest now": `/v1/source-items/held`, by `source_id`.
 * - `processing` / `failed` / `integrated` — `/v1/ingestion`, by `source_id`, so two
 *   mailboxes are two sets of counts rather than one shared by kind. That route leaves
 *   held items out (motet#91), so a waiting item is never counted as work in flight.
 */
export type SourceCounts = {
  held: number
  processing: number
  failed: number
  integrated: number
}

export function countsFor(
  source: Source,
  held: HeldSourceItem[],
  ingestion: IngestionItem[],
): SourceCounts {
  const here = ingestion.filter((item) => item.source_id === source.id)
  return {
    held: held.filter((item) => item.source_id === source.id).length,
    processing: here.filter((item) => item.state === 'pending').length,
    failed: here.filter((item) => item.state === 'failed').length,
    integrated: here.filter((item) => item.state === 'integrated').length,
  }
}

/**
 * What the most recent poll found, in a sentence. `null` when no poll has run.
 *
 * `seen` is what the poll listed that matches the filter and `queued` is how many of
 * those were new and went on to extraction. It is not a count of items held: extraction
 * can still skip a message that turns out to be a receipt, so the Waiting tile is the
 * number to trust. `caught_up: false` is a first sync of a large backlog part-way through
 * — each poll queues the next (motet#94).
 */
export function describeLastSync(source: Source): string | null {
  const last = source.last_sync
  if (!last) return null
  if (last.error) return `The last sync gave up: ${last.error}`
  const looked =
    last.seen === 0
      ? 'No new messages.'
      : last.queued === 0
        ? `Looked at ${last.seen} message${last.seen === 1 ? '' : 's'}; none were new.`
        : `Looked at ${last.seen} message${last.seen === 1 ? '' : 's'}; ${last.queued} ${last.queued === 1 ? 'was' : 'were'} new.`
  return last.caught_up ? looked : `${looked} Still catching up: each sync queues the next.`
}

/**
 * OAuth scopes as short names. The full URL is what the API reports and what a reader
 * would have to look up; "read-only mail" is what was granted.
 */
export function describeScope(scope: string): string {
  const known: Record<string, string> = {
    'https://www.googleapis.com/auth/gmail.readonly': 'read-only mail',
    'gmail.readonly': 'read-only mail',
    openid: 'identity',
    email: 'email address',
    profile: 'profile',
  }
  return known[scope] ?? scope
}

/** "3 minutes ago" / "2 days ago", against a clock the caller passes so a test can pin it. */
export function relativeTime(iso: string, now: number = Date.now()): string {
  const then = new Date(iso).getTime()
  if (!Number.isFinite(then)) return iso
  const seconds = Math.max(0, Math.round((now - then) / 1000))
  if (seconds < 45) return 'just now'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? '' : 's'} ago`
  const hours = Math.round(minutes / 60)
  if (hours < 36) return `${hours} hour${hours === 1 ? '' : 's'} ago`
  const days = Math.round(hours / 24)
  return `${days} day${days === 1 ? '' : 's'} ago`
}

export type SyncProgress = NonNullable<Source['sync_progress']>

/** Stages in which something is still happening, so the screen keeps asking. */
const IN_FLIGHT = new Set(['queued', 'retrying', 'connecting', 'listing', 'fetching'])

/** Whether a sync is running or waiting to — the button is busy and the screen polls. */
export const syncInFlight = (progress: SyncProgress | null | undefined): boolean =>
  !!progress && IN_FLIGHT.has(progress.stage)

/**
 * A sync in flight, in words and a bar — `SourceSyncProgress` on the API, which adds up
 * the whole poll chain and joins it to the job queue (motet#94's chain made visible).
 *
 * - `headline` is the step: where the sync is, never a bare "Syncing…".
 * - `count` is ingested-vs-left once there is a count to state, and says "at least" while
 *   the search is still listing, because the total can only grow until it ends.
 * - `fraction` drives a determinate bar; `null` is an indeterminate one, for the steps
 *   before anything has been found. A bar with no denominator would be a made-up number.
 * - `tone` is `stalled` when the API says nothing will run it — the one case where a
 *   moving bar would be the never-infer-"no errors"-from-"no data" trap (motet#38).
 */
export type SyncDescription = {
  headline: string
  count: string | null
  detail: string | null
  fraction: number | null
  tone: 'working' | 'stalled' | 'done' | 'error'
}

const n = (value: number): string => value.toLocaleString('en-US')
const messages = (value: number): string => `${n(value)} message${value === 1 ? '' : 's'}`

export function describeSyncProgress(progress: SyncProgress): SyncDescription {
  const stalled = progress.waiting_on_worker
  const noWorker =
    'No worker has run in the last five minutes, so this will not move until one does.'
  const atLeast = progress.found_is_lower_bound ? 'at least ' : ''
  const count =
    progress.found > 0
      ? `Pulled in ${n(progress.pulled_in)} of ${atLeast}${n(progress.found)} · ${n(progress.remaining)} left`
      : null
  const fraction = progress.found > 0 ? Math.min(1, progress.pulled_in / progress.found) : null
  const failedNote =
    progress.failed > 0
      ? `${messages(progress.failed)} could not be fetched and ${progress.failed === 1 ? 'was' : 'were'} left out.`
      : null
  const working = (headline: string, detail: string | null, bar: number | null = fraction): SyncDescription => ({
    headline,
    count,
    detail: stalled ? noWorker : detail,
    fraction: bar,
    tone: stalled ? 'stalled' : 'working',
  })

  switch (progress.stage) {
    case 'queued':
      return working('Waiting for a worker to start the sync', null, null)
    case 'retrying':
      return working(
        'Could not reach the mailbox — trying again',
        progress.error ? `Last attempt: ${progress.error}` : null,
        null,
      )
    case 'connecting':
      return working('Connecting to the mailbox', null, null)
    case 'listing':
      return working(
        progress.found > 0
          ? `Listing messages · found ${atLeast}${n(progress.found)} new so far`
          : 'Listing messages',
        progress.listed > 0
          ? `Looked through ${messages(progress.listed)} matching the filter; more pages to go.`
          : null,
      )
    case 'fetching':
      return working(
        `Fetching and extracting · ${messages(progress.found)} found`,
        failedNote ?? 'The search is finished; each message is fetched and its article extracted.',
      )
    case 'done':
      return {
        headline:
          progress.found > 0 ? `Sync finished · ${messages(progress.found)} pulled in` : 'Sync finished · nothing new',
        count: null,
        detail: failedNote ?? (progress.found > 0 ? 'New items are held for you to ingest.' : null),
        fraction: progress.found > 0 ? 1 : null,
        tone: 'done',
      }
    case 'failed':
      return {
        headline: 'The sync gave up',
        count:
          progress.found > 0
            ? `Pulled in ${n(progress.pulled_in)} of ${n(progress.found)} found before it stopped` +
              (progress.remaining > 0 ? ` · ${n(progress.remaining)} still being fetched` : '')
            : null,
        detail: progress.error,
        fraction,
        tone: 'error',
      }
    default:
      // A stage this build does not know — a newer API. Said plainly rather than guessed at.
      return working('Syncing', null, null)
  }
}
