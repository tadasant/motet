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

import { type ReactNode, useState } from 'react'

import { ApiError, api } from '../api/client'
import { MicGlyph, Motif, Voices } from '../brand/Brand'
import { beginConsent, redirectUri, rememberState } from '../oauth'

type Status = { kind: 'idle' } | { kind: 'busy' } | { kind: 'error'; message: string }

export function SignIn({
  /** Overridden only by tests: jsdom cannot navigate. */
  navigate = beginConsent,
  tokenField,
}: {
  navigate?: (url: string) => void
  /** The API token disclosure, which the landing offers below the fold as the other key. */
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

  // The landing is the reference hero (brand/polyphony/index.html): the headline, the
  // positioning, the motif, and one primary action — which is signing in, because that is
  // what starting to listen takes here. What it does and the other way in sit below it.
  return (
    <>
      <section className="hero" aria-labelledby="hero-heading">
        <div className="hero-copy">
          <p className="eyebrow">
            <Voices />
            A motet: many voices, one piece.
          </p>
          <h1 id="hero-heading" className="headline">
            Many voices.
            <br />
            One thing <em>worth hearing.</em>
          </h1>
          <p className="subhead">
            Motet turns content you trust into an interactive podcast you can listen to on the
            go. When a story catches you, just ask: it goes deeper, then picks the episode back
            up.
          </p>
          <div className="ctas">
            <button type="button" className="btn-primary" onClick={signIn} disabled={status.kind === 'busy'}>
              <span className="play-glyph" aria-hidden="true" />
              {status.kind === 'busy' ? 'Redirecting…' : 'Start listening'}
            </button>
            <a className="btn-text" href="#signin-heading">
              Other ways in →
            </a>
          </div>
          {status.kind === 'error' && (
            <p className="error" role="alert">
              {status.message}
            </p>
          )}
          <div className="hero-meta">
            <span>Content you trust</span>
            <span>Interactive: just ask</span>
            <span>Made for on the go</span>
          </div>
        </div>

        <Motif caption="Four sources you trust, sung as one podcast.">
          {/* A picture of the player, as the reference draws it: the score is something you
              play. Not a control — the real one is on an episode. */}
          <div className="transport" aria-hidden="true">
            <span className="play" />
            <div className="scrub">
              <span className="t">04:12</span>
              <div className="track">
                <div className="played" style={{ width: '13.3%' }} />
                <div className="knob" style={{ left: '13.3%' }} />
              </div>
              <span className="t">31:40</span>
            </div>
            <span className="pills">
              <span className="pill-button pill-static">1.2×</span>
              <span className="pill-button pill-static mic">
                <MicGlyph />
                hold to ask
              </span>
            </span>
          </div>
          <p className="now-playing" aria-hidden="true">Now playing · Saturday’s episode · 9 sources · 31 min</p>
        </Motif>
      </section>

      <section className="door-panel" aria-labelledby="signin-heading">
        <div>
          <p className="kicker">Your account</p>
          <h2 id="signin-heading">Sign in</h2>
          <div className="row">
            <button type="button" onClick={signIn} disabled={status.kind === 'busy'}>
              Sign in with Google
            </button>
          </div>
          <p className="hint">
            Start listening signs you in with Google too. Motet has one account, and signing in is
            how this browser proves it may use it — only addresses the deployment lists are
            accepted.
          </p>
          <p className="hint">
            {/* Printed because a mismatch is invisible from in here: Google matches this
                string exactly and rejects anything unregistered on its own error page. In dev
                that means reaching the app at localhost, not 127.0.0.1. */}
            Google will return you to <code className="feed-url">{redirectUri()}</code>, which
            has to be registered on the OAuth client.
          </p>
          <p className="hint">
            No Google account handy? The API token still works — open{' '}
            <strong>API token</strong> and paste it.
          </p>
        </div>
        {tokenField}
      </section>
    </>
  )
}
