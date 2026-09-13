// What a browser sees at /app/signed-in, which is the iOS app's https handoff link.
//
// Normally nobody sees this. The app's sign-in sheet is watching for this exact path and
// closes on it, so the navigation never finishes loading a page. It renders for the cases
// where that did not happen: the link opened in an ordinary browser, on a device without
// the app, or on an iOS too old to wait for an https callback.
//
// It deliberately shows nothing about the sign-in and reads nothing out of the URL. The
// code in the query is single-use, expires in two minutes, and is worthless without the
// verifier that never left the app — there is nothing for this page to do with it.
//
// It does take the code out of the address, because this is the one path where the link
// reaches a *server*: the query lands in an access log, and rides along as `Referer` on
// every subresource this page pulls. Cheap, and it costs nothing that is used.

import { useEffect } from 'react'

export function AppHandoff({ onDone }: { onDone: () => void }) {
  useEffect(() => {
    if (window.location.search) {
      window.history.replaceState(null, '', window.location.pathname)
    }
  }, [])

  return (
    <section aria-labelledby="app-handoff-heading">
      <h1 id="app-handoff-heading">Back to the app</h1>
      <p className="hint">
        This link belongs to the Motet app on your iPhone. If the app did not open, go back to
        it and sign in from there — this page cannot finish it for you.
      </p>
      <div className="row">
        <button type="button" onClick={onDone}>
          Open Motet on the web
        </button>
      </div>
    </section>
  )
}
