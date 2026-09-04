// Screen 4: where source items come from, and the only way to connect a mailbox.
//
// Phase 2 shipped `POST /v1/sources/connect` and the whole PKCE + incremental-consent
// Gmail client, and then nothing called it — so Gmail ingestion stayed on the fake
// fixture mailbox in every environment no matter what credentials existed. This screen is
// that missing half.
//
// Gmail is the only provider offered, deliberately: the API answers 400 for anything
// else, because X bookmarks are not built and the API tier is a spend decision that has
// not been made. A disabled "Connect X" button would be a promise the backend refuses to
// keep.

import { useCallback, useEffect, useState } from 'react'

import { ApiError, type Source, api } from '../api/client'
import { beginConsent, redirectUri, rememberState } from '../oauth'

/** Gmail's own default, shown so the field reads as "override this" rather than "fill me in". */
const DEFAULT_QUERY = 'category:updates OR category:promotions'

type Status = { kind: 'idle' } | { kind: 'busy' } | { kind: 'error'; message: string }

/**
 * `paste` is the one kind with nothing behind it: no credential to grant and nothing to
 * fetch on a schedule. Every other kind is a mailbox or a feed reached with a grant, so
 * an unrecognised one is read that way rather than as a second paste box — X bookmarks,
 * if the API tier is ever bought, is an OAuth source too.
 */
const PASTE_KIND = 'paste'

/** Whether `connected` says anything about this source. Pasted text has no credential. */
const needsConsent = (source: Source): boolean => source.kind !== PASTE_KIND

/** Whether anything ever polls it. Pasted text arrives when you paste it, and not before. */
const isPollable = (source: Source): boolean => source.kind !== PASTE_KIND

/**
 * One phrase for the two booleans the API returns, read against the kind that decides
 * which of them applies.
 *
 * An OAuth source is created **inactive and unconnected** and only activates once a
 * credential lands, so "waiting for consent" is the correct reading of a row that appears
 * the instant you press Connect — not a failure. Saying so here is what keeps it from
 * looking like one.
 *
 * The paste source is the opposite on both counts: it is created **active**, and it never
 * connects because there is no credential to connect. `connected: false` is not a state it
 * is passing through, so reading it as one told the user that the one source actually in
 * use was an abandoned OAuth attempt (motet#39). `active` is the field that describes it,
 * and the only one.
 */
function statusOf(source: Source): string {
  if (!needsConsent(source)) return source.active ? 'ready' : 'paused'
  if (!source.connected) return 'waiting for consent'
  return source.active ? 'connected' : 'paused'
}

export function Sources({
  /** Overridden only by tests: jsdom cannot navigate. */
  navigate = beginConsent,
}: {
  navigate?: (url: string) => void
}) {
  const [sources, setSources] = useState<Source[] | null>(null)
  const [name, setName] = useState('Gmail')
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState<Status>({ kind: 'idle' })
  const [error, setError] = useState('')

  const refresh = useCallback(() => {
    api
      .sources()
      .then((next) => {
        setSources(next)
        setError('')
      })
      .catch((err) => {
        setSources([])
        setError(err instanceof ApiError ? err.message : String(err))
      })
  }, [])

  useEffect(refresh, [refresh])

  const connect = async (event: React.FormEvent) => {
    event.preventDefault()
    setStatus({ kind: 'busy' })
    try {
      const connection = await api.connectSource(name.trim(), query.trim(), redirectUri())
      // Remembered before the redirect, not after: once `navigate` runs, nothing else in
      // this tab gets to execute.
      rememberState(connection.state)
      navigate(connection.authorization_url)
    } catch (err) {
      // A 503 here is the dormant case, not a bug — in real mode with no Google OAuth
      // client provisioned the API says exactly which variable is missing, so showing
      // its message beats inventing one.
      setStatus({
        kind: 'error',
        message: err instanceof ApiError ? err.message : String(err),
      })
    }
  }

  return (
    <section aria-labelledby="sources-heading">
      <h2 id="sources-heading">Sources</h2>
      <p className="hint">
        Where source items come from. Pasting in needs nothing; a mailbox needs your
        consent, which Google asks for on its own page.
      </p>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {sources === null ? (
        <p className="hint">Loading…</p>
      ) : sources.length === 0 ? (
        <p className="hint">No sources yet.</p>
      ) : (
        <ul className="items">
          {sources.map((source) => {
            // A poll time is only news about something that gets polled: "Never polled"
            // under the paste row read as a stalled fetch rather than as the absence of
            // one. Composed rather than branched inline because the paste row then has
            // nothing left to say here at all, and an empty line is its own small lie.
            const details = [
              isPollable(source) &&
                (source.last_polled_at
                  ? `Last polled ${new Date(source.last_polled_at).toLocaleString()}`
                  : 'Never polled'),
              source.scopes.length > 0 && source.scopes.join(' '),
            ].filter((part): part is string => typeof part === 'string')
            return (
              <li key={source.id}>
                <div className="item-head">
                  <strong>{source.name}</strong>
                  <span className="hint">
                    {source.kind} · {statusOf(source)}
                  </span>
                </div>
                {source.last_error && (
                  <p className="error" role="alert">
                    {source.last_error}
                  </p>
                )}
                {details.length > 0 && <p className="hint">{details.join(' · ')}</p>}
              </li>
            )
          })}
        </ul>
      )}

      <h3>Connect Gmail</h3>
      <p className="hint">
        Motet asks for read-only access to your mail and stores only a refresh token,
        sealed — nothing in the API can read it back. The source row is created before you
        leave for Google and stays inactive until you come back having said yes, so a
        mailbox listed above as &ldquo;waiting for consent&rdquo; is an abandoned attempt
        rather than a broken one. Pasting in never appears that way: it has no credential
        to wait for.
      </p>
      <form onSubmit={connect}>
        <label htmlFor="source-name">Name</label>
        <input
          id="source-name"
          value={name}
          required
          maxLength={200}
          placeholder="Gmail"
          onChange={(e) => setName(e.target.value)}
        />
        <label htmlFor="source-query">Gmail search (optional)</label>
        <input
          id="source-query"
          value={query}
          maxLength={500}
          placeholder={DEFAULT_QUERY}
          onChange={(e) => setQuery(e.target.value)}
        />
        <p className="hint">
          Which messages count as newsletters, in Gmail&rsquo;s own search syntax. Left
          blank it is <code>{DEFAULT_QUERY}</code>, which needs no setup.
        </p>
        <button type="submit" disabled={status.kind === 'busy' || !name.trim()}>
          {status.kind === 'busy' ? 'Redirecting…' : 'Connect Gmail'}
        </button>
      </form>
      <p className="hint">
        {/* Printed because a mismatch is invisible from in here: Google matches this
            string exactly and rejects anything unregistered on its own error page. In dev
            that means reaching the app at localhost, not 127.0.0.1. */}
        Google will return you to <code className="feed-url">{redirectUri()}</code>, which
        has to be registered on the OAuth client.
      </p>
      {status.kind === 'error' && (
        <p className="error" role="alert">
          {status.message}
        </p>
      )}
    </section>
  )
}
