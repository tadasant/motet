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
 * - `disconnected` — no credential, but it *has* polled before: the credential was
 *   forgotten through `DELETE /v1/sources/{id}/credentials`. The API does not record
 *   this state on its own — a disconnected row and an abandoned one are both
 *   `connected: false, active: false` — so `last_polled_at` is the tell. A row that
 *   never polled was never connected.
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
  if (!source.connected) return source.last_polled_at ? 'disconnected' : 'awaiting_consent'
  if (source.last_error) return 'error'
  return source.active ? 'connected' : 'paused'
}

/** The row status in words, for a pill or a caption. */
export function rowStatusLabel(status: RowStatus): string {
  switch (status) {
    case 'connected':
      return 'Connected'
    case 'paused':
      return 'Paused'
    case 'error':
      return 'Error'
    case 'awaiting_consent':
      return 'Awaiting consent'
    case 'disconnected':
      return 'Disconnected'
    case 'ready':
      return 'Always on'
  }
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
 * What one source has pulled in, and where it is.
 *
 * Three of the four numbers are exact and one is not, and the panel says which:
 *
 * - `held` — extracted, waiting for "ingest now". `/v1/source-items/held` carries
 *   `source_id`, so this is filtered to the row.
 * - `processing` / `failed` / `integrated` — `/v1/ingestion` carries `source_kind` and
 *   **not** `source_id`, so these can only be filtered by kind. Exact while there is one
 *   source of the kind, and an over-count the moment there are two mailboxes. Listed as
 *   an API gap in proto/issues/08.
 *
 * A held item also appears in `/v1/ingestion` as `pending` (issue 03: `list_ingestion`
 * does not yet exclude jobless pending items), so `processing` subtracts held ids rather
 * than counting a waiting item as work in flight.
 */
export type SourceCounts = {
  held: number
  processing: number
  failed: number
  integrated: number
  /** Whether `processing`/`failed`/`integrated` were filtered by kind rather than by row. */
  byKind: boolean
}

export function countsFor(
  source: Source,
  held: HeldSourceItem[],
  ingestion: IngestionItem[],
  allSources: Source[],
): SourceCounts {
  const heldHere = held.filter((item) => item.source_id === source.id)
  const heldIds = new Set(heldHere.map((item) => item.id))
  const ofKind = ingestion.filter((item) => item.source_kind === source.kind && !heldIds.has(item.id))
  return {
    held: heldHere.length,
    processing: ofKind.filter((item) => item.state === 'pending').length,
    failed: ofKind.filter((item) => item.state === 'failed').length,
    integrated: ofKind.filter((item) => item.state === 'integrated').length,
    byKind: allSources.filter((row) => row.kind === source.kind).length > 1,
  }
}

/**
 * Google's `error` codes, in the user's words — the same reading `OAuthCallback.tsx`
 * gives them, kept here so the Sources screen's copy about an unfinished consent agrees
 * with the callback page's.
 *
 * `access_denied` is not a fault: it is someone pressing Cancel, which is a supported
 * answer to being asked for a mailbox, and it must not read like a crash.
 */
export function explainOAuthError(error: string, description = ''): string {
  if (error === 'access_denied') {
    return 'You cancelled on Google’s page, so nothing was connected. Nothing was changed.'
  }
  return description ? `Google refused this: ${error} — ${description}` : `Google refused this: ${error}`
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
