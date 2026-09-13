// Screen 4: where source items come from — an integrations catalog, and the only way to
// connect a mailbox.
//
// PROTOTYPE (proto/local-ux): the screen used to be a list of rows and a form. It is now
// a catalog of what Motet can pull from — Gmail, Paste, and two honestly labelled
// "Coming soon" — with one panel under the grid for the integration you pick: the
// account(s) behind it, what each has pulled in and where that went, Sync now, Disconnect,
// and the connect form. The connect *flow* is untouched (`sources/ConnectGmail.tsx`,
// `oauth.ts`, the callback branch in App.tsx); what changed is how it is presented.
//
// The thing this screen has to teach, after issue 03, is one sentence: connected means
// "pulled in and held", not "processed". So every count of held items here says "waiting
// for you" and points at the Backlog, where the ingest button is.

import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  ApiError,
  type HeldSourceItem,
  type IngestionItem,
  type ProcessingStatus,
  type Source,
  api,
} from '../api/client'
import { beginConsent } from '../oauth'
import { ConnectGmail } from './sources/ConnectGmail'
import { IntegrationCard } from './sources/IntegrationCard'
import { SourceDetail } from './sources/SourceDetail'
import { CATALOG, type Integration, type IntegrationId, integrationById } from './sources/catalog'
import { IntegrationIcon } from './sources/icons'
import { cardStatus, countsFor, rowStatus } from './sources/status'

/**
 * Move to another section the way the shell does — `pushState` plus a `popstate` — so
 * `usePath` in App.tsx picks it up without this screen importing the shell. A `popstate`
 * nobody listens to is harmless: the URL still changes, and a reload lands there.
 */
function navigateTo(path: string): void {
  window.history.pushState({}, '', path)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

/** How often the derived counts refresh while something is in flight. */
const REFRESH_MS = 10_000

export function Sources({
  /** Overridden only by tests: jsdom cannot navigate. */
  navigate = beginConsent,
  /** Overridden only by tests, so relative times are deterministic. */
  now,
}: {
  navigate?: (url: string) => void
  now?: number
}) {
  const [sources, setSources] = useState<Source[] | null>(null)
  const [held, setHeld] = useState<HeldSourceItem[]>([])
  const [ingestion, setIngestion] = useState<IngestionItem[]>([])
  const [processing, setProcessing] = useState<ProcessingStatus | null>(null)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<IntegrationId | null>(null)
  const [connectAnother, setConnectAnother] = useState(false)

  /**
   * The sources list is the primary fetch and the only one that can blank the screen.
   * Held, ingestion and processing are what the detail panel is *derived* from, and each
   * is best-effort: a 404 from an older API loses a count, never the catalog.
   */
  const refresh = useCallback(async () => {
    const [list, heldItems, ingestionItems, processingStatus] = await Promise.all([
      api.sources().then(
        (next) => {
          setError('')
          return next
        },
        (err: unknown) => {
          setError(err instanceof ApiError ? err.message : String(err))
          return [] as Source[]
        },
      ),
      api.heldSourceItems().catch(() => [] as HeldSourceItem[]),
      api.ingestion().catch(() => [] as IngestionItem[]),
      api.processing().catch(() => null),
    ])
    setSources(list)
    setHeld(heldItems)
    setIngestion(ingestionItems)
    setProcessing(processingStatus)
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // Keep the counts honest while work is moving: a panel saying "3 processing" for ten
  // minutes after they landed is the same small lie the ingestion panel exists to remove.
  const inFlight = ingestion.some((item) => item.state === 'pending')
  useEffect(() => {
    if (!inFlight) return
    const timer = window.setInterval(() => void refresh(), REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [inFlight, refresh])

  const rowsFor = useCallback(
    (integration: Integration): Source[] =>
      integration.kind === null ? [] : (sources ?? []).filter((row) => row.kind === integration.kind),
    [sources],
  )

  // The empty state: no mailbox has ever been connected, so Gmail is the first thing to
  // do and the card says so. Read off the rows rather than off "sources.length === 0",
  // because `src_paste` is always there and a fresh account still has one row.
  const gmailRows = rowsFor(integrationById('gmail'))
  const fresh = sources !== null && !gmailRows.some((row) => row.connected)

  // Open Gmail's panel on its own when there is exactly one thing it could show: a fresh
  // account gets the connect form without a click, and one connected mailbox gets its
  // detail. Two mailboxes, or anything the person has closed, stay closed.
  const [autoOpened, setAutoOpened] = useState(false)
  useEffect(() => {
    if (sources === null || autoOpened) return
    setAutoOpened(true)
    if (fresh || gmailRows.filter((row) => row.connected).length === 1) setSelected('gmail')
  }, [sources, autoOpened, fresh, gmailRows])

  const selectedIntegration = selected ? integrationById(selected) : null
  const selectedRows = useMemo(
    () => (selectedIntegration ? rowsFor(selectedIntegration) : []),
    [selectedIntegration, rowsFor],
  )

  const toggle = (id: IntegrationId) => {
    setConnectAnother(false)
    setSelected((current) => (current === id ? null : id))
  }

  return (
    <section aria-label="Sources" className="sources">
      <p className="lead">
        Where your reading comes from. Connect a source and Motet pulls new items in on its
        own; <strong>nothing is processed until you ingest it</strong> from the Backlog.
      </p>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {sources === null ? (
        <p className="hint">Loading…</p>
      ) : (
        <div className="integration-grid">
          {CATALOG.map((integration) => (
            <IntegrationCard
              key={integration.id}
              integration={integration}
              rows={rowsFor(integration)}
              selected={selected === integration.id}
              highlighted={fresh && integration.id === 'gmail'}
              onSelect={() => toggle(integration.id)}
              onOpenPaste={() => navigateTo('/paste')}
            />
          ))}
        </div>
      )}

      {selectedIntegration && sources !== null && (
        <div
          className="integration-panel"
          id={`integration-panel-${selectedIntegration.id}`}
          role="region"
          aria-label={`${selectedIntegration.name} details`}
        >
          <div className="integration-panel-head">
            <IntegrationIcon id={selectedIntegration.id} />
            <div>
              <h3>{selectedIntegration.name}</h3>
              <p className="hint">{selectedIntegration.detail}</p>
            </div>
            <button type="button" className="linkish close" onClick={() => setSelected(null)}>
              Close
            </button>
          </div>

          {selectedIntegration.id === 'gmail' && (
            <GmailPanel
              rows={selectedRows}
              allSources={sources}
              held={held}
              ingestion={ingestion}
              processing={processing}
              onRefresh={refresh}
              navigate={navigate}
              connectAnother={connectAnother}
              onConnectAnother={setConnectAnother}
              {...(now === undefined ? {} : { now })}
            />
          )}

          {selectedIntegration.id === 'paste' && (
            <PastePanel
              rows={selectedRows}
              allSources={sources}
              held={held}
              ingestion={ingestion}
              processing={processing}
              onRefresh={refresh}
              {...(now === undefined ? {} : { now })}
            />
          )}
        </div>
      )}
    </section>
  )
}

/**
 * Gmail's panel: every mailbox row in full, then the connect form — on its own when there
 * is nothing connected, behind "Connect another mailbox" when there is.
 *
 * Order is "needs attention first": an errored or connected mailbox above an abandoned
 * consent attempt, so the row that matters is the one at the top.
 */
function GmailPanel({
  rows,
  allSources,
  held,
  ingestion,
  processing,
  onRefresh,
  navigate,
  connectAnother,
  onConnectAnother,
  now,
}: {
  rows: Source[]
  allSources: Source[]
  held: HeldSourceItem[]
  ingestion: IngestionItem[]
  processing: ProcessingStatus | null
  onRefresh: () => Promise<void>
  navigate: (url: string) => void
  connectAnother: boolean
  onConnectAnother: (open: boolean) => void
  now?: number
}) {
  const rank: Record<ReturnType<typeof rowStatus>, number> = {
    error: 0,
    connected: 1,
    paused: 2,
    disconnected: 3,
    awaiting_consent: 4,
    ready: 5,
  }
  const ordered = [...rows].sort((a, b) => rank[rowStatus(a)] - rank[rowStatus(b)])
  const anyConnected = rows.some((row) => row.connected)
  const status = cardStatus(integrationById('gmail'), rows)

  return (
    <>
      {ordered.length > 0 && (
        <ul className="source-rows">
          {ordered.map((source) => (
            <li key={source.id}>
              <SourceDetail
                source={source}
                counts={countsFor(source, held, ingestion, allSources)}
                processing={processing}
                onRefresh={onRefresh}
                onGoToBacklog={() => navigateTo('/backlog')}
                {...(now === undefined ? {} : { now })}
              />
            </li>
          ))}
        </ul>
      )}

      {!anyConnected ? (
        <ConnectGmail navigate={navigate} />
      ) : connectAnother ? (
        <div className="connect-another">
          <div className="row">
            <h4>Connect another mailbox</h4>
            <button type="button" className="linkish" onClick={() => onConnectAnother(false)}>
              Cancel
            </button>
          </div>
          <ConnectGmail navigate={navigate} compact />
        </div>
      ) : (
        <p className="hint connect-another-hint">
          {status === 'connected' ? 'One mailbox is connected. ' : ''}
          <button type="button" className="linkish" onClick={() => onConnectAnother(true)}>
            Connect another mailbox
          </button>
        </p>
      )}
    </>
  )
}

/** Paste's panel: the built-in row, its counts, and where to go to use it. */
function PastePanel({
  rows,
  allSources,
  held,
  ingestion,
  processing,
  onRefresh,
  now,
}: {
  rows: Source[]
  allSources: Source[]
  held: HeldSourceItem[]
  ingestion: IngestionItem[]
  processing: ProcessingStatus | null
  onRefresh: () => Promise<void>
  now?: number
}) {
  return (
    <>
      {rows.map((source) => (
        <SourceDetail
          key={source.id}
          source={source}
          counts={countsFor(source, held, ingestion, allSources)}
          processing={processing}
          onRefresh={onRefresh}
          onGoToBacklog={() => navigateTo('/backlog')}
          {...(now === undefined ? {} : { now })}
        />
      ))}
      <p className="hint">
        <button type="button" className="linkish" onClick={() => navigateTo('/paste')}>
          Go to Paste in
        </button>{' '}
        to add something now.
      </p>
    </>
  )
}
