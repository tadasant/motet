// What the web app's /oauth/callback shows for a mailbox or connector consent that this
// tab did not begin — in practice, one begun in the iOS app, finishing inside the system
// sign-in sheet (2026-09-19).
//
// The sheet is waiting for `motet://consent`, so this page sends the browser there at once
// and the sheet closes on it; the app then finishes the consent with its own session. The
// page is only ever *seen* if that navigation goes nowhere: an ordinary browser, a phone
// without the app, or a consent that really was begun in another tab of this browser —
// which is why "Finish here instead" exists. It is the exchange this page used to make
// unconditionally, and the API's state row decides it either way.

import { useEffect, useRef } from 'react'

import { type OAuthCallback, appConsentHandoffUrl } from '../oauth'

export function AppConsentHandoff({
  callback,
  onFinishHere,
  navigate = (url: string) => window.location.assign(url),
}: {
  callback: Exclude<OAuthCallback, { kind: 'empty' }>
  onFinishHere: () => void
  /** Overridden only by tests: jsdom cannot navigate. */
  navigate?: (url: string) => void
}) {
  const url = appConsentHandoffUrl(callback)
  // Once, even under StrictMode's double effect: the link is the whole answer.
  const sent = useRef(false)

  useEffect(() => {
    if (sent.current) return
    sent.current = true
    navigate(url)
  }, [navigate, url])

  return (
    <section aria-labelledby="app-consent-heading">
      <h1 id="app-consent-heading">Back to the Motet app</h1>
      <p className="hint" role="status">
        This consent was started in the Motet app, so it finishes there. If the app did not take
        over, open it below.
      </p>
      <div className="row">
        <button type="button" className="primary" onClick={() => navigate(url)}>
          Open the Motet app
        </button>
        <button type="button" onClick={onFinishHere}>
          Finish here instead
        </button>
      </div>
    </section>
  )
}
