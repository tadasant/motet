// Where Google drops the user after the consent screen: /oauth/callback.
//
// The browser arrives here with a fresh page load and no memory of the app it left, so
// everything this screen needs is either in the query string or in storage. It exchanges
// the code once, says what happened in a sentence, and hands the user back to the normal
// UI — it is a doorway, not a screen anyone should linger on.

import { useEffect, useRef, useState } from 'react'

import { ApiError, type Source, api } from '../api/client'
import { type OAuthCallback as Callback, stateMatches, takeState } from '../oauth'

type Status =
  | { kind: 'busy' }
  | { kind: 'done'; source: Source }
  | { kind: 'error'; message: string }

/**
 * Google's `error` codes, in the user's words.
 *
 * `access_denied` is not a fault — it is someone pressing Cancel, which is a supported
 * answer to being asked for a mailbox — so it must not read like a crash. Anything else
 * falls through to the raw code, which is more use to whoever has to look it up than a
 * generic apology would be.
 */
function explain(error: string, description: string): string {
  if (error === 'access_denied') {
    return 'You did not grant access, so nothing was connected. Nothing was changed.'
  }
  return description ? `Google refused this: ${error} — ${description}` : `Google refused this: ${error}`
}

export function OAuthCallback({
  callback,
  onDone,
}: {
  callback: Callback
  onDone: () => void
}) {
  const [status, setStatus] = useState<Status>({ kind: 'busy' })
  // StrictMode runs an effect twice on mount, and an authorization code is single-use:
  // the second exchange would fail on a state the first one already consumed and
  // overwrite a success with "already used". A ref survives that remount, so the
  // exchange happens once.
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
          'started, so it was not used. Start again from Sources.',
      })
      return
    }

    api
      .completeOAuth(callback.state, callback.code)
      .then((source) => setStatus({ kind: 'done', source }))
      .catch((err) =>
        setStatus({ kind: 'error', message: err instanceof ApiError ? err.message : String(err) }),
      )
  }, [callback])

  return (
    <section aria-labelledby="callback-heading">
      <h2 id="callback-heading">Connecting</h2>

      {callback.kind === 'denied' && (
        <p className="hint" role="status">
          {explain(callback.error, callback.description)}
        </p>
      )}

      {callback.kind === 'empty' && (
        <p className="hint" role="status">
          There is nothing to finish here. Start from Sources.
        </p>
      )}

      {callback.kind === 'granted' && status.kind === 'busy' && (
        <p className="hint" role="status">
          Finishing up — exchanging the code and sealing the token.
        </p>
      )}
      {callback.kind === 'granted' && status.kind === 'done' && (
        <p className="ok" role="status">
          {status.source.name} is connected. Its first poll is already queued; newsletters
          join the backlog as the workers get to them.
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
