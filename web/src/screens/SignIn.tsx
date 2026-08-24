// The door. What you see when this browser holds no token.
//
// It exists because the previous answer was "open the API token disclosure and paste a
// shared secret", which is a fine thing to do at a desk and a miserable one on a phone
// halfway round a dog walk. Signing in with Google puts a *session* token in the same
// slot that secret went into, so nothing downstream of `client.ts` changes.
//
// **The button is not the security.** This deployment's Google consent screen is
// published and unverified, so anyone with a Google account can finish the flow; the API
// checks the verified address against MOTET_ALLOWED_EMAILS before it mints anything, and
// an unset allowlist denies everybody. That is why a refusal here reads as "that account
// is not allowed" rather than as a bug.

import { useState } from 'react'

import { ApiError, api } from '../api/client'
import { beginConsent, redirectUri, rememberState } from '../oauth'

type Status = { kind: 'idle' } | { kind: 'busy' } | { kind: 'error'; message: string }

export function SignIn({
  /** Overridden only by tests: jsdom cannot navigate. */
  navigate = beginConsent,
}: {
  navigate?: (url: string) => void
}) {
  const [status, setStatus] = useState<Status>({ kind: 'idle' })

  const signIn = async () => {
    setStatus({ kind: 'busy' })
    try {
      const started = await api.startLogin(redirectUri())
      // Remembered before the redirect, not after: once `navigate` runs, nothing else in
      // this tab gets to execute.
      rememberState(started.state)
      navigate(started.authorization_url)
    } catch (err) {
      // A 503 here is the deployment saying sign-in is not configured — no allowlist, or
      // no OAuth client in real mode — and it names the variable. That message is worth
      // more than anything this screen could invent.
      setStatus({ kind: 'error', message: err instanceof ApiError ? err.message : String(err) })
    }
  }

  return (
    <section aria-labelledby="signin-heading">
      <h2 id="signin-heading">Sign in</h2>
      <p className="hint">
        Motet has one account. Signing in with Google is how this browser proves it is
        allowed to use it — only addresses the deployment lists are accepted.
      </p>

      <div className="row">
        <button type="button" onClick={signIn} disabled={status.kind === 'busy'}>
          {status.kind === 'busy' ? 'Redirecting…' : 'Sign in with Google'}
        </button>
      </div>

      {status.kind === 'error' && (
        <p className="error" role="alert">
          {status.message}
        </p>
      )}

      <p className="hint">
        {/* Printed because a mismatch is invisible from in here: Google matches this
            string exactly and rejects anything unregistered on its own error page. In dev
            that means reaching the app at localhost, not 127.0.0.1. */}
        Google will return you to <code className="feed-url">{redirectUri()}</code>, which
        has to be registered on the OAuth client.
      </p>
      <p className="hint">
        No Google account handy? The API token still works — open{' '}
        <strong>API token</strong> above and paste it.
      </p>
    </section>
  )
}
