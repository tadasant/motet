// Where Google drops the user after a *sign-in*: /oauth/callback, with a `login.` state.
//
// The sibling of `OAuthCallback`, which handles the same path for a mailbox connection.
// Two screens rather than one branching screen because the two do different things with
// the result — one seals a mailbox token server-side and reports a source, the other
// hands this browser the credential it will use from now on — and because the sentences a
// user reads are different in every case.
//
// Everything it needs is in the query string or in storage: the browser arrived on a
// fresh page load with no memory of the app it left.

import { useEffect, useRef, useState } from 'react'

import { ApiError, api } from '../api/client'
import { type OAuthCallback as Callback, stateMatches, takeState } from '../oauth'

type Status =
  | { kind: 'busy' }
  | { kind: 'done'; email: string }
  | { kind: 'error'; message: string }

/**
 * Google's `error` codes, in the user's words.
 *
 * `access_denied` is somebody pressing Cancel, which is a supported answer to being asked
 * who they are. It must not read like a crash.
 */
function explain(error: string, description: string): string {
  if (error === 'access_denied') {
    return 'You did not finish signing in, so nothing changed.'
  }
  return description ? `Google refused this: ${error} — ${description}` : `Google refused this: ${error}`
}

export function SignInCallback({
  callback,
  onSignedIn,
  onDone,
}: {
  callback: Callback
  /** Hands the session token up to the app, which stores it and stops showing the door. */
  onSignedIn: (token: string) => void
  onDone: () => void
}) {
  const [status, setStatus] = useState<Status>({ kind: 'busy' })
  // StrictMode runs an effect twice on mount, and an authorization code is single-use:
  // the second exchange would fail on a state the first one already consumed and
  // overwrite a success with "already used". A ref survives that remount.
  const exchanged = useRef(false)

  useEffect(() => {
    if (callback.kind !== 'granted' || exchanged.current) return
    exchanged.current = true

    const expected = takeState()
    if (!stateMatches(expected, callback.state)) {
      setStatus({
        kind: 'error',
        message:
          'This callback belongs to a different sign-in than the one this tab started, ' +
          'so it was not used. Try signing in again.',
      })
      return
    }

    api
      .completeLogin(callback.state, callback.code)
      .then((session) => {
        // Stored before anything is rendered about it: this is the credential every
        // later request carries, and a success message with no token behind it would be
        // a lie the next screen would then contradict.
        onSignedIn(session.token)
        setStatus({ kind: 'done', email: session.email })
      })
      .catch((err) =>
        setStatus({
          kind: 'error',
          // A 403 here is the allowlist doing its job — a verified Google account that
          // this deployment does not list. The API's own sentence says so.
          message: err instanceof ApiError ? err.message : String(err),
        }),
      )
  }, [callback, onSignedIn])

  return (
    <section aria-labelledby="signin-callback-heading">
      <h2 id="signin-callback-heading">Signing in</h2>

      {callback.kind === 'denied' && (
        <p className="hint" role="status">
          {explain(callback.error, callback.description)}
        </p>
      )}

      {callback.kind === 'granted' && status.kind === 'busy' && (
        <p className="hint" role="status">
          Finishing up — checking who you are.
        </p>
      )}
      {callback.kind === 'granted' && status.kind === 'done' && (
        <p className="ok" role="status">
          Signed in as {status.email}.
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
