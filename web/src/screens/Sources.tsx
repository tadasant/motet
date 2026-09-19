// Screen 4: where source items come from — an integrations catalog, and the only way to
// connect a mailbox.
//
// It used to be a list of rows and a form. It is a catalog of what Motet can pull from —
// Gmail, Paste, and two honestly labelled "Coming soon" — with one panel for the
// integration you pick, opened in the grid directly under its card: the account(s) behind
// it, what each has pulled in and where that went, Sync now, Disconnect, and the connect
// form (motet#90). The connect
// *flow* is untouched (`sources/ConnectGmail.tsx`, `oauth.ts`, the callback branch in
// App.tsx); what changed is how it is presented.
//
// The thing this screen has to teach, since a connected source holds what it pulls in
// (motet#91), is one sentence: connected means "pulled in and held", not "processed". So
// every count of held items here says "waiting for you" and points at the Backlog, where
// the ingest button is.

import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'

import {
  ApiError,
  type HeldSourceItem,
  type IngestionItem,
  type Source,
  api,
} from '../api/client'
import { beginConsent } from '../oauth'
import { ConnectGmail } from './sources/ConnectGmail'
import { IntegrationCard } from './sources/IntegrationCard'
import { SourceDetail } from './sources/SourceDetail'
import { CATALOG, type Integration, type IntegrationId, integrationById } from './sources/catalog'
import { IntegrationIcon } from './sources/icons'
import { countsFor, rowStatus, syncInFlight, syncMoving } from './sources/status'

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

/** How often while a mailbox sync is running, so its progress bar moves as the work does. */
const SYNC_REFRESH_MS = 2_000

export function Sources({
  /**
   * What Google said when it refused the last consent, if it refused one — carried here
   * from the callback page by App (motet#98). Shown once at the top, because this is the
   * screen the flow lands back on, and what the attempt left — the card's "Consent not
   * finished" and a row notice hedging "you cancelled … or closed it" — cannot say which.
   */
  notice = '',
  /** Overridden only by tests: jsdom cannot navigate. */
  navigate = beginConsent,
  /** Overridden only by tests, so relative times are deterministic. */
  now,
}: {
  notice?: string
  navigate?: (url: string) => void
  now?: number
}) {
  const [sources, setSources] = useState<Source[] | null>(null)
  const [held, setHeld] = useState<HeldSourceItem[]>([])
  const [ingestion, setIngestion] = useState<IngestionItem[]>([])
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<IntegrationId | null>(null)
  const [connectAnother, setConnectAnother] = useState(false)
  const autoOpened = useRef(false)
  // Set when a *person* opens a panel, so the panel is brought into view once it renders.
  // Not for the automatic open on first load: scrolling a page the moment it arrives is a
  // page that jumps under somebody's thumb.
  const scrollToPanel = useRef(false)
  const panelRef = useRef<HTMLDivElement>(null)

  /**
   * The sources list is the primary fetch and the only one that can blank the screen.
   * Held and ingestion are what the detail panel's counts are *derived* from, and each
   * is best-effort: a 404 from an older API loses a count, never the catalog.
   */
  const refresh = useCallback(async () => {
    let loaded = false
    const [list, heldItems, ingestionItems] = await Promise.all([
      api.sources().then(
        (next) => {
          setError('')
          loaded = true
          return next
        },
        (err: unknown) => {
          setError(err instanceof ApiError ? err.message : String(err))
          return [] as Source[]
        },
      ),
      api.heldSourceItems().catch(() => [] as HeldSourceItem[]),
      api.ingestion().catch(() => [] as IngestionItem[]),
    ])
    // A failed re-fetch keeps the rows already on screen. Replacing them with nothing
    // would read as a fresh account — Gmail highlighted, every panel unmounted — on one
    // transient error during a watch that re-fetches every two seconds.
    setSources((previous) => (loaded ? list : (previous ?? [])))
    setHeld(heldItems)
    setIngestion(ingestionItems)
    // Open Gmail's panel on its own when there is exactly one thing it could show: a
    // fresh account gets the connect form without a click, and one connected mailbox gets
    // its detail. Two connected mailboxes, or anything the person has closed, stay closed. Decided
    // here, in the same batch as the rows, so the panel does not open a frame late; and
    // only on the first load that succeeded, so a failed one cannot pass for "fresh".
    if (loaded && !autoOpened.current) {
      autoOpened.current = true
      const mailboxes = list.filter((row) => row.kind === integrationById('gmail').kind)
      if (mailboxes.filter((row) => row.connected).length <= 1) setSelected('gmail')
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // Keep the counts honest while work is moving: a panel saying "3 processing" for ten
  // minutes after they landed is the same small lie the ingestion panel exists to remove.
  // A sync in flight polls faster and for as long as it runs: the progress is the server's,
  // so a twenty-minute first sync is watched to the end rather than given up on (motet#94).
  const inFlight = ingestion.some((item) => item.state === 'pending')
  // A sync nothing will run is still watched, at the ordinary interval: it moves when a
  // worker appears, and a tab left open on a stalled sync must not poll every two seconds.
  const syncing = (sources ?? []).some((row) => syncInFlight(row.sync_progress))
  const moving = (sources ?? []).some((row) => syncMoving(row.sync_progress))
  useEffect(() => {
    if (!inFlight && !syncing) return
    const timer = window.setInterval(() => void refresh(), moving ? SYNC_REFRESH_MS : REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [inFlight, syncing, moving, refresh])

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

  const selectedIntegration = selected ? integrationById(selected) : null
  const selectedRows = useMemo(
    () => (selectedIntegration ? rowsFor(selectedIntegration) : []),
    [selectedIntegration, rowsFor],
  )

  const toggle = (id: IntegrationId) => {
    setConnectAnother(false)
    scrollToPanel.current = selected !== id
    setSelected((current) => (current === id ? null : id))
  }

  // Pressing Connect or Manage has to *show* something. The panel is the whole answer to
  // that press, and before this it could open entirely off-screen — under three more cards
  // on a phone — so all anyone saw was the button flipping between Connect and Close.
  // Optional call: jsdom has no `scrollIntoView`.
  useEffect(() => {
    if (!selected || !scrollToPanel.current) return
    scrollToPanel.current = false
    const still = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
    panelRef.current?.scrollIntoView?.({ behavior: still ? 'auto' : 'smooth', block: 'start' })
  }, [selected])

  return (
    <section aria-label="Sources" className="sources">
      <p className="lead">
        Where the content you trust comes from. Connect a source and Motet pulls new items in on its
        own; <strong>nothing is processed until you ingest it</strong> from the Backlog.
      </p>

      {/* `role="status"` and not `alert`: pressing Cancel on Google's page is a supported
          answer, and the sentence says what was *not* changed rather than what broke. */}
      {notice && (
        <p className="hint consent-notice" role="status">
          {notice}
        </p>
      )}

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {sources === null ? (
        <p className="hint">Loading…</p>
      ) : (
        // The panel is rendered *inside* the grid, straight after the card that opened it,
        // spanning every column (`grid-auto-flow: dense` backfills the card beside it). It
        // used to follow the whole grid, which put it below the fold on a phone — see
        // `toggle`. DOM order is also focus order: the panel comes right after its button.
        <div className="integration-grid">
          {CATALOG.map((integration) => (
            <Fragment key={integration.id}>
              <IntegrationCard
                integration={integration}
                rows={rowsFor(integration)}
                selected={selected === integration.id}
                highlighted={fresh && integration.id === 'gmail'}
                onSelect={() => toggle(integration.id)}
                onOpenPaste={() => navigateTo('/paste')}
              />
              {selectedIntegration?.id === integration.id && (
                <div
                  ref={panelRef}
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
                      held={held}
                      ingestion={ingestion}
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
                      held={held}
                      ingestion={ingestion}
                      onRefresh={refresh}
                      {...(now === undefined ? {} : { now })}
                    />
                  )}
                </div>
              )}
            </Fragment>
          ))}
        </div>
      )}
    </section>
  )
}

/**
 * Gmail's panel: the connect form first when nothing is connected, then every mailbox row
 * in full; with a mailbox connected, the rows first and the form behind "Connect another
 * mailbox".
 *
 * Order is "needs attention first": an errored or connected mailbox above an abandoned
 * consent attempt, so the row that matters is the one at the top.
 */
function GmailPanel({
  rows,
  held,
  ingestion,
  onRefresh,
  navigate,
  connectAnother,
  onConnectAnother,
  now,
}: {
  rows: Source[]
  held: HeldSourceItem[]
  ingestion: IngestionItem[]
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
  const connectedCount = rows.filter((row) => row.connected).length
  const anyConnected = connectedCount > 0

  const rowList = ordered.length > 0 && (
    <ul className="source-rows">
      {ordered.map((source) => (
        <li key={source.id}>
          <SourceDetail
            source={source}
            counts={countsFor(source, held, ingestion)}
            onRefresh={onRefresh}
            onGoToBacklog={() => navigateTo('/backlog')}
            {...(now === undefined ? {} : { now })}
          />
        </li>
      ))}
    </ul>
  )

  // Nothing connected: the form is the thing to do, so it comes first. Below it are only
  // abandoned attempts and disconnected mailboxes, and each of those is a full detail block
  // — above the form, one of them pushed "Connect Gmail" a screen and a half down a phone.
  if (!anyConnected) {
    return (
      <>
        <ConnectGmail navigate={navigate} />
        {rowList}
      </>
    )
  }

  return (
    <>
      {rowList}
      {connectAnother ? (
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
          {connectedCount === 1 ? 'One mailbox is connected. ' : `${connectedCount} mailboxes are connected. `}
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
  held,
  ingestion,
  onRefresh,
  now,
}: {
  rows: Source[]
  held: HeldSourceItem[]
  ingestion: IngestionItem[]
  onRefresh: () => Promise<void>
  now?: number
}) {
  return (
    <>
      {rows.map((source) => (
        <SourceDetail
          key={source.id}
          source={source}
          counts={countsFor(source, held, ingestion)}
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
