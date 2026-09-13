// The connect flow's first half, presented: an explainer, two fields, one button.
//
// **The flow itself is unchanged.** `POST /v1/sources/connect` mints the source row and
// the consent URL, `rememberState` keeps the `state` for the callback, and `navigate`
// hands the browser to Google — exactly as before this screen became a catalog. What
// changed is what is said around it: that the read is read-only, that nothing is
// processed until the person asks (issue 03), and what a 503 means.

import { useState } from 'react'

import { ApiError, api } from '../../api/client'
import { beginConsent, redirectUri, rememberState } from '../../oauth'

/** Motet's default search (`motet_sources.gmail.DEFAULT_QUERY`), shown so the field reads as "override this". */
export const DEFAULT_QUERY = 'category:updates OR category:promotions'

type Status = { kind: 'idle' } | { kind: 'busy' } | { kind: 'error'; message: string; status: number }

export function ConnectGmail({
  navigate = beginConsent,
  compact = false,
}: {
  /** Overridden only by tests: jsdom cannot navigate. */
  navigate?: (url: string) => void
  /** A second mailbox under an already-connected one: shorter explainer. */
  compact?: boolean
}) {
  const [name, setName] = useState('Gmail')
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState<Status>({ kind: 'idle' })

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
      setStatus({
        kind: 'error',
        message: err instanceof ApiError ? err.message : String(err),
        status: err instanceof ApiError ? err.status : -1,
      })
    }
  }

  return (
    <div className="connect-gmail">
      {!compact && (
        <>
          <h4>Connect a mailbox</h4>
          <p className="connect-explainer">
            We&rsquo;ll read newsletters matching your filter — read-only, and only the
            messages the search matches. What arrives is pulled in and held.{' '}
            <strong>Nothing is processed until you choose to ingest it</strong> from the
            Backlog.
          </p>
          <p className="hint">
            Motet stores only a refresh token, sealed: nothing in the API can read it back.
            Google asks for consent on its own page and returns you here.
          </p>
        </>
      )}
      <form onSubmit={connect} aria-label="Connect Gmail">
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
          Which messages count as newsletters, in Gmail&rsquo;s own search syntax. Left blank
          it is <code>{DEFAULT_QUERY}</code>, which needs no setup.
        </p>
        <button
          type="submit"
          className="primary"
          disabled={status.kind === 'busy' || !name.trim()}
        >
          {status.kind === 'busy' ? 'Redirecting to Google…' : 'Connect Gmail'}
        </button>
      </form>
      {status.kind === 'error' && <ConnectError status={status.status} message={status.message} />}
      <p className="hint redirect-note">
        {/* Printed because a mismatch is invisible from in here: Google matches this
            string exactly and rejects anything unregistered on its own error page. In dev
            that means reaching the app at localhost, not 127.0.0.1. */}
        Google returns you to <code className="feed-url">{redirectUri()}</code>, which has to
        be registered on the OAuth client.
      </p>
    </div>
  )
}

/**
 * What a refused connect means, by status.
 *
 * A 503 is the dormant case, not a bug — real mode with no Google OAuth client
 * provisioned, or a vault that will not open — and the API names the variable that is
 * missing, so its message is shown rather than replaced. A 0 is the network layer: the
 * client already turned that into a sentence naming the URL.
 */
function ConnectError({ status, message }: { status: number; message: string }) {
  const lead =
    status === 503
      ? 'This deployment cannot connect Gmail right now. That is configuration, not you:'
      : status === 400
        ? 'The API refused the request:'
        : status === 0
          ? 'The request never reached the API:'
          : 'Connecting failed:'
  return (
    <div className="error connect-error" role="alert">
      <p>{lead}</p>
      <p className="reason">{message}</p>
    </div>
  )
}
