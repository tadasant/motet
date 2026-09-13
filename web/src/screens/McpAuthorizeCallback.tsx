// Where Google drops the user during an *MCP client's* authorization: /oauth/callback,
// with an `mcp.` state (motet#111).
//
// The third sibling of `OAuthCallback` and `SignInCallback`, and the one that does not
// finish anything by itself. The API verifies who signed in and mints the client's
// authorization code, but the code is inside `redirect_url` and reaches the client only
// when this browser navigates there — so **the navigation is the grant**, and it happens
// on a click and never on its own. Before that click the screen says which client is
// asking, where the grant goes, and which account it will act as.
//
// This tab did not start the flow — the MCP client did, through the API's `/authorize` —
// so there is no remembered state to check. The API consumes its state row exactly once,
// which is the check.

import { useEffect, useRef, useState } from 'react'

import { ApiError, type McpAuthorization, api } from '../api/client'
import { type OAuthCallback as Callback, beginConsent } from '../oauth'

type Status =
  | { kind: 'busy' }
  | { kind: 'done'; authorization: McpAuthorization }
  | { kind: 'error'; message: string }

/**
 * Google's `error` codes, in the user's words.
 *
 * Nothing reaches the MCP client on a refusal: the API never saw a code, so it has
 * nothing to send and no deny URL to hand back. The client simply stops waiting.
 */
function explain(error: string, description: string): string {
  if (error === 'access_denied') {
    return 'You did not finish signing in, so the agent was not connected. Nothing was sent to it.'
  }
  const refused = description ? `${error} — ${description}` : error
  return `Google refused this: ${refused}. The agent was not connected, and nothing was sent to it.`
}

export function McpAuthorizeCallback({
  callback,
  onDone,
  navigate = beginConsent,
}: {
  callback: Callback
  onDone: () => void
  /**
   * Hands the browser to the MCP client. A prop because jsdom cannot navigate, as with
   * `SignIn`'s `beginConsent`; it is the same one line.
   */
  navigate?: (url: string) => void
}) {
  const [status, setStatus] = useState<Status>({ kind: 'busy' })
  // Once a button is pressed the page is on its way out; a second press would send the
  // browser somewhere a second time, possibly the other way.
  const [decided, setDecided] = useState(false)
  // StrictMode runs an effect twice on mount, and the state is single-use: the second
  // exchange would fail on a row the first one already consumed and overwrite the
  // question with "already used". A ref survives that remount.
  const exchanged = useRef(false)

  useEffect(() => {
    if (callback.kind !== 'granted' || exchanged.current) return
    exchanged.current = true

    api
      .completeMcpAuthorization(callback.state, callback.code)
      .then((authorization) => setStatus({ kind: 'done', authorization }))
      .catch((err) =>
        setStatus({
          kind: 'error',
          // A 403 is the allowlist refusing a verified account that this deployment does
          // not list; a 400 is a state already used or expired. The API's sentence says which.
          message: err instanceof ApiError ? err.message : String(err),
        }),
      )
  }, [callback])

  const decide = (url: string) => {
    setDecided(true)
    navigate(url)
  }

  return (
    <section aria-labelledby="mcp-callback-heading">
      <h1 id="mcp-callback-heading">Connect an agent</h1>

      {callback.kind === 'denied' && (
        <p className="hint" role="status">
          {explain(callback.error, callback.description)}
        </p>
      )}

      {callback.kind === 'empty' && (
        <p className="hint" role="status">
          There is nothing to finish here. Start again from the agent.
        </p>
      )}

      {callback.kind === 'granted' && status.kind === 'busy' && (
        <p className="hint" role="status">
          Checking who you are and which agent is asking.
        </p>
      )}

      {callback.kind === 'granted' && status.kind === 'done' && (
        <>
          <p>
            <strong>{status.authorization.client_name}</strong> wants to use Motet as{' '}
            {status.authorization.email}.
          </p>
          <p className="hint">
            Allowing it sends you back to {status.authorization.redirect_host}, and the agent can
            then read your backlog, make episodes and change settings, as you. Nothing is sent to
            it until you choose.
          </p>
          <div className="row">
            <button
              type="button"
              className="primary"
              onClick={() => decide(status.authorization.redirect_url)}
              disabled={decided}
            >
              Allow
            </button>
            <button
              type="button"
              onClick={() => decide(status.authorization.deny_url)}
              disabled={decided}
            >
              Deny
            </button>
          </div>
        </>
      )}

      {callback.kind === 'granted' && status.kind === 'error' && (
        <p className="error" role="alert">
          {status.message}
        </p>
      )}

      {/* Not beside Allow and Deny: leaving without choosing would leave the agent waiting
          on an answer it never gets. Deny is how to say no. */}
      {!(callback.kind === 'granted' && status.kind !== 'error') && (
        <div className="row">
          <button type="button" onClick={onDone}>
            Back to Motet
          </button>
        </div>
      )}
    </section>
  )
}
