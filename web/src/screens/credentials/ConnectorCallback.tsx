// Where an MCP authorization server drops the user after consent: /oauth/callback with a
// `connector.` state. The same doorway as OAuthCallback, finishing at a different route.

import { useEffect, useRef, useState } from 'react'

import { ApiError, api, type Connector } from '../../api/client'
import { type OAuthCallback as Callback, stateMatches, takeState } from '../../oauth'

type Status =
  | { kind: 'busy' }
  | { kind: 'done'; connector: Connector }
  | { kind: 'error'; message: string }

function explain(error: string, description: string): string {
  if (error === 'access_denied') {
    return 'You did not approve the connector, so nothing was authorized. Nothing was changed.'
  }
  return description
    ? `The server refused this: ${error} — ${description}`
    : `The server refused this: ${error}`
}

export function ConnectorCallback({ callback, onDone }: { callback: Callback; onDone: () => void }) {
  const [status, setStatus] = useState<Status>({ kind: 'busy' })
  // StrictMode mounts twice and a code is single-use; see OAuthCallback.
  const exchanged = useRef(false)

  useEffect(() => {
    if (callback.kind !== 'granted' || exchanged.current) return
    exchanged.current = true

    const expected = takeState()
    if (!stateMatches(expected, callback.state)) {
      setStatus({
        kind: 'error',
        message:
          'This callback belongs to a different authorization than the one this tab ' +
          'started, so it was not used. Start again from Credentials.',
      })
      return
    }

    api
      .completeConnectorOAuth(callback.state, callback.code, callback.iss)
      .then((connector) => setStatus({ kind: 'done', connector }))
      .catch((err) =>
        setStatus({ kind: 'error', message: err instanceof ApiError ? err.message : String(err) }),
      )
  }, [callback])

  return (
    <section aria-labelledby="connector-callback-heading">
      <h2 id="connector-callback-heading">Authorizing</h2>

      {callback.kind === 'denied' && (
        <p className="hint" role="status">
          {explain(callback.error, callback.description)}
        </p>
      )}
      {callback.kind === 'empty' && (
        <p className="hint" role="status">
          There is nothing to finish here. Start from Credentials.
        </p>
      )}
      {callback.kind === 'granted' && status.kind === 'busy' && (
        <p className="hint" role="status">
          Finishing up — exchanging the code and sealing the tokens.
        </p>
      )}
      {callback.kind === 'granted' && status.kind === 'done' && (
        <p className="ok" role="status">
          {status.connector.label} is authorized. The ingestion agent can use it from now on.
        </p>
      )}
      {callback.kind === 'granted' && status.kind === 'error' && (
        <p className="error" role="alert">
          {status.message}
        </p>
      )}

      <div className="row">
        <button type="button" onClick={onDone}>
          Back to Motet
        </button>
      </div>
    </section>
  )
}
