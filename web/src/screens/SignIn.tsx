// The door. What you see when this browser holds no token.
//
// It exists because the previous answer was "open the API token disclosure and paste a
// shared secret", which is a fine thing to do at a desk and a miserable one on a phone
// halfway round a dog walk. Signing in with Google puts a *session* token in the same
// slot that secret went into, so nothing downstream of `client.ts` changes.
//
// **It is a door and not a landing.** It used to render the reference hero above the
// sign-in panel — the headline, the positioning, the motif, a "Start listening" CTA —
// which was the only marketing surface Motet had. Since the `site/` landing went live on
// `getmotet.com` (motet#110's brand, built from the same reference page), that hero was
// the same pitch a second time, in front of the one thing somebody who typed the app's
// address came for. So the pitch lives at `getmotet.com` and this is the sign-in, with a
// link out for anyone who arrived wanting the other thing.
//
// **The button is not the security.** This deployment's Google consent screen is
// published and unverified, so anyone with a Google account can finish the flow; the API
// checks the verified address against MOTET_ALLOWED_EMAILS before it mints anything, and
// an unset allowlist denies everybody. That is why a refusal here reads as "that account
// is not allowed" rather than as a bug.

import { type ReactNode, useState } from 'react'

import { ApiError, api } from '../api/client'
import { Voices } from '../brand/Brand'
import { beginConsent, redirectUri, rememberState } from '../oauth'

type Status = { kind: 'idle' } | { kind: 'busy' } | { kind: 'error'; message: string }

/**
 * The product's own site, not a deployment's. A literal because it is a fact about Motet
 * rather than about an environment — every environment's door points at the one landing
 * page — so it needs no variable, and a variable would need a private-repo change to set.
 */
const LANDING_URL = 'https://getmotet.com/'

export function SignIn({
  /** Overridden only by tests: jsdom cannot navigate. */
  navigate = beginConsent,
  tokenField,
}: {
  navigate?: (url: string) => void
  /** The API token disclosure, which the door offers as the other key. */
  tokenField?: ReactNode
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

  // One action, and the error directly under it. There used to be two buttons calling
  // this same function — "Start listening" in the hero and "Sign in with Google" below
  // it — and only the hero one had somewhere to print a failure, so a refusal pressed on
  // the lower button appeared a screenful away.
  return (
    <section className="signin" aria-labelledby="signin-heading">
      <p className="eyebrow">
        <Voices />
        A motet: many voices, one piece.
      </p>
      <h1 id="signin-heading" className="signin-heading">
        Sign in
      </h1>
      <p className="subhead">Motet has one account. This is how this browser proves it may use it.</p>
      <button type="button" className="btn-primary" onClick={signIn} disabled={status.kind === 'busy'}>
        {status.kind === 'busy' ? 'Redirecting…' : 'Sign in with Google'}
      </button>
      {status.kind === 'error' && (
        <p className="error" role="alert">
          {status.message}
        </p>
      )}
      <p className="hint">Only addresses this deployment lists are accepted.</p>
      <p className="hint">
        {/* Printed because a mismatch is invisible from in here: Google matches this
            string exactly and rejects anything unregistered on its own error page. In dev
            that means reaching the app at localhost, not 127.0.0.1. */}
        Google returns you to <code className="feed-url">{redirectUri()}</code>, which has to be
        registered on the OAuth client.
      </p>
      <p className="hint">
        No Google account handy? Open <strong>API token</strong> and paste one.
      </p>
      {tokenField}
      <p className="signin-away">
        Wanted the pitch rather than the app?{' '}
        <a href={LANDING_URL}>getmotet.com</a>
      </p>
    </section>
  )
}
